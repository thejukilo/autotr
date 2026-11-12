"""Backtest engine: replays cached 1m history for many symbols in timestamp order."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date

from autotr.config import Config
from autotr.data.replay import MarketHistory, load_history
from autotr.engine.core import TradingCore
from autotr.models import MINUTE_MS, EquityPoint
from autotr.sim.filters import FilterBook

log = logging.getLogger(__name__)

EQUITY_SAMPLE_MS = 15 * MINUTE_MS


@dataclass
class BacktestResult:
    core: TradingCore
    equity: list[EquityPoint]
    start_ts: int
    end_ts: int
    symbols: list[str]
    elapsed_s: float
    funding_source: dict[str, str] = field(default_factory=dict)


def run_backtest(cfg: Config, start: date, end: date, symbols: list[str] | None = None,
                 warmup_days: int | None = None, progress: bool = True,
                 history: MarketHistory | None = None) -> BacktestResult:
    """Run over [start, end] (inclusive UTC days). Warm-up days before `start` (default: enough
    for the structure timeframe) feed indicators and the scanner without trading."""
    h = history or load_history(cfg, start, end, symbols or cfg.universe.symbols, warmup_days)
    core = TradingCore(cfg, FilterBook.load(cfg.data.dir))
    ex = core.exchange

    equity: list[EquityPoint] = []
    next_sample = h.trade_start_ts
    t0 = time.perf_counter()
    total = h.n_minutes
    report_every = max(total // 20, 1)
    last_ts = h.trade_start_ts

    for n, (ts, bars) in enumerate(h.timeline()):
        last_ts = ts
        if ts < h.trade_start_ts:
            for bar, _ in bars:
                core.warmup_bar(bar)
            continue
        # Same order as the live engine: rescan with data up to the previous minute, then
        # exchange fills → funding → features/strategy for each bar.
        if ts > h.trade_start_ts:
            core.maybe_rescan(ts)
        for bar, rate in bars:
            ex.on_bar(bar)
            if rate is not None:
                ex.on_funding(bar.symbol, rate, bar.open, ts)
            core.on_bar_closed(bar)
        if ts >= next_sample:
            equity.append(EquityPoint(ts, ex.equity(), ex.balance, len(ex.positions)))
            next_sample += EQUITY_SAMPLE_MS
        if progress and n % report_every == 0 and n:
            log.info("backtest %3.0f%%  trades=%d  equity=%.2f", 100 * n / total, len(ex.trades), ex.equity())

    # Close anything still open at the last price so the result is fully realised.
    end_ts = last_ts + MINUTE_MS
    for sym in list(ex.positions):
        ex.close_market(sym, ex.last_price[sym], end_ts, "end_of_backtest")
    ex.pending.clear()
    equity.append(EquityPoint(end_ts, ex.equity(), ex.balance, 0))

    return BacktestResult(core, equity, h.trade_start_ts, end_ts, h.symbols, time.perf_counter() - t0,
                          h.funding_source)
