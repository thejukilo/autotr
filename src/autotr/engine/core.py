"""TradingCore — the wiring shared by the backtest and live engines.

Per closed 1m bar:  exchange fills / exits  →  features update  →  (if symbol is active)
strategy signal  →  risk manager  →  pending entry on the paper exchange.
"""

from __future__ import annotations

import logging
from typing import Callable

from autotr.config import Config
from autotr.market.features import SymbolState
from autotr.market.scanner import Scanner
from autotr.models import Bar, Signal
from autotr.risk.manager import RiskManager
from autotr.sim.exchange import PaperExchange
from autotr.sim.filters import FilterBook
from autotr.strategy.scalper import ScalperStrategy

log = logging.getLogger(__name__)


class TradingCore:
    def __init__(self, cfg: Config, filters: FilterBook | None = None, exchange: PaperExchange | None = None):
        self.cfg = cfg
        self.filters = filters or FilterBook()
        self.exchange = exchange or PaperExchange(cfg.exchange, cfg.account.starting_balance)
        self.strategy = ScalperStrategy(cfg.strategy, cfg.exchange)
        self.risk = RiskManager(cfg.risk, cfg.exchange, cfg.strategy, self.filters)
        self.scanner = Scanner(cfg.scanner)
        self.states: dict[str, SymbolState] = {}
        self.signals: list[Signal] = []
        self.on_signal: list[Callable[[Signal, bool], None]] = []
        self.exchange.on_trade.append(self.risk.on_trade)

    def state(self, symbol: str) -> SymbolState:
        st = self.states.get(symbol)
        if st is None:
            st = self.states[symbol] = SymbolState(symbol, self.cfg.strategy, self.cfg.scanner)
        return st

    def warmup_bar(self, bar: Bar) -> None:
        """Feed history without trading (live start-up)."""
        self.state(bar.symbol).on_bar(bar)

    def on_bar_closed(self, bar: Bar) -> Signal | None:
        """Process a closed bar *after* the exchange has seen it (fills/exits)."""
        st = self.state(bar.symbol)
        st.on_bar(bar)
        self.risk.update_clock(bar.close_time, self.exchange.equity())
        if bar.symbol not in self.scanner.active:
            return None
        sig = self.strategy.on_bar(st)
        if sig is None:
            return None
        self.signals.append(sig)
        entry = self.risk.evaluate(sig, self.exchange)
        accepted = entry is not None and self.exchange.submit_entry(entry)
        for cb in self.on_signal:
            cb(sig, accepted)
        return sig

    def maybe_rescan(self, ts: int, volume_24h: dict[str, float] | None = None) -> bool:
        if not self.scanner.due(ts):
            return False
        self.scanner.scan(self.states, ts, volume_24h)
        return True
