"""Live (paper) trading engine: consumes a feed of market events and trades the paper account.

The account (wallet, open positions, risk-day state) is persisted to SQLite after every minute
and every fill, so stopping and restarting the bot resumes where it left off.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import AsyncIterator, Protocol

from autotr.config import Config
from autotr.data.events import Event, Funding, KlineClosed, Quote, Volumes24h, WarmupBars
from autotr.engine.core import TradingCore
from autotr.models import MINUTE_MS, EquityPoint, Signal, Trade
from autotr.report.tradelog import LiveStore
from autotr.sim.exchange import Position
from autotr.sim.filters import FilterBook

log = logging.getLogger("autotr.live")

EQUITY_EVERY_MS = 5 * MINUTE_MS
STATUS_EVERY_MS = 15 * MINUTE_MS
STATE_KEY = "engine"


class Feed(Protocol):
    bar_fills: bool

    def events(self) -> AsyncIterator[Event]: ...


def _hhmm(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%m-%d %H:%M")


class LiveEngine:
    def __init__(self, cfg: Config, store: LiveStore | None, filters: FilterBook | None = None,
                 bar_fills: bool = False, save_every_ms: int = MINUTE_MS):
        self.cfg = cfg
        self.store = store
        self.bar_fills = bar_fills
        self.save_every_ms = save_every_ms
        self._next_save = 0
        self.core = TradingCore(cfg, filters)
        self.ex = self.core.exchange
        self.volumes: dict[str, float] | None = None
        self.last_minute: int | None = None
        self.last_seen: dict[str, int] = {}
        self._next_equity = 0
        self._next_status = 0
        self.resumed = self._restore()  # may set last_minute / scanner state

        self.ex.on_trade.append(self._on_trade)
        self.ex.on_open.append(self._on_open)
        self.core.on_signal.append(self._on_signal)

    # ------------------------------------------------------------ persistence
    def _restore(self) -> bool:
        if self.store is None:
            return False
        state = self.store.load_state(STATE_KEY)
        if not state:
            return False
        self.ex.load_state(state["exchange"])
        self.core.risk.load_state(state["risk"])
        sc = state.get("scanner", {})
        self.core.scanner.active = set(sc.get("active", []))
        self.core.scanner.last_scan_ts = sc.get("last_scan_ts")
        self.last_minute = state.get("last_minute")
        self.core.filters.load_defaults_state(state.get("default_filters", {}))
        log.info("resumed paper account: balance $%.2f, %d open position(s)", self.ex.balance, len(self.ex.positions))
        return True

    def save(self) -> None:
        if self.store is not None:
            sc = self.core.scanner
            self.store.save_state(STATE_KEY, {
                "exchange": self.ex.to_state(),
                "risk": self.core.risk.to_state(),
                "scanner": {"active": sorted(sc.active), "last_scan_ts": sc.last_scan_ts},
                "last_minute": self.last_minute,
                "default_filters": self.core.filters.defaults_state(),
            })

    # ------------------------------------------------------------ callbacks
    def _on_trade(self, t: Trade) -> None:
        if self.store is not None:
            self.store.add_trade(t)
        log.info("CLOSE %-5s %-12s %s @ %.6g -> %.6g  net $%+.3f (%+.2fR)  balance $%.2f",
                 t.side.value, t.symbol, t.exit_reason, t.entry_price, t.exit_price, t.net_pnl,
                 t.r_multiple, self.ex.balance)
        self.save()

    def _on_open(self, p: Position) -> None:
        log.info("OPEN  %-5s %-12s qty %g @ %.6g  stop %.6g  tp %.6g  risk $%.2f  | %s",
                 p.side.value, p.symbol, p.qty, p.entry_price, p.stop, p.take_profit, p.risk_usd, p.reason)
        self.save()

    def _on_signal(self, s: Signal, accepted: bool) -> None:
        log.debug("signal %s %s %s (%s)", s.symbol, s.side.value, "accepted" if accepted else "rejected", s.reason)

    # ------------------------------------------------------------ events
    def handle(self, ev: Event) -> None:
        if isinstance(ev, KlineClosed):
            self._on_kline(ev)
        elif isinstance(ev, Quote):
            if self.ex.busy(ev.symbol):
                self.ex.on_quote(ev.symbol, ev.bid, ev.ask, ev.ts)
            else:
                self.ex.last_price[ev.symbol] = (ev.bid + ev.ask) / 2
        elif isinstance(ev, Funding):
            amount = self.ex.on_funding(ev.symbol, ev.rate, ev.mark_price, ev.ts)
            if amount:
                log.info("FUNDING %s rate %.4f%% -> $%+.4f", ev.symbol, ev.rate * 100, amount)
        elif isinstance(ev, WarmupBars):
            for bar in ev.bars:
                if bar.open_time > self.last_seen.get(bar.symbol, -1):
                    self.last_seen[bar.symbol] = bar.open_time
                    self.core.warmup_bar(bar)
        elif isinstance(ev, Volumes24h):
            self.volumes = ev.quote_volume

    def _on_kline(self, ev: KlineClosed) -> None:
        bar = ev.bar
        if bar.open_time <= self.last_seen.get(bar.symbol, -1):
            return
        self.last_seen[bar.symbol] = bar.open_time
        if ev.backfill:
            self.core.warmup_bar(bar)
            return
        if self.last_minute is not None and bar.open_time > self.last_minute:
            self._on_new_minute(bar.open_time)
        if self.last_minute is None or bar.open_time > self.last_minute:
            self.last_minute = bar.open_time
        if self.bar_fills:
            self.ex.on_bar(bar)
        else:
            self.ex.last_price.setdefault(bar.symbol, bar.close)
        if ev.funding_rate is not None:
            self.ex.on_funding(bar.symbol, ev.funding_rate, bar.open, bar.open_time)
        self.core.on_bar_closed(bar)

    def _on_new_minute(self, ts: int) -> None:
        """Runs once per minute, before processing the first candle of the new minute."""
        if self.core.maybe_rescan(ts, self.volumes):
            act = sorted(self.core.scanner.active)
            log.debug("watching %s", ", ".join(act))
        if ts >= self._next_save:
            self._next_save = ts + self.save_every_ms
            self.save()
        if ts >= self._next_equity:
            self._next_equity = ts + EQUITY_EVERY_MS
            if self.store is not None:
                self.store.add_equity(EquityPoint(ts, self.ex.equity(), self.ex.balance, len(self.ex.positions)))
        if ts >= self._next_status:
            self._next_status = ts + STATUS_EVERY_MS
            rows = {r.symbol: r for r in self.core.scanner.last_rows}
            ranked = sorted((rows[s] for s in self.core.scanner.active if s in rows),
                            key=lambda r: -r.volatility)
            active = ", ".join(f"{r.symbol}({r.volatility:.2%})" for r in ranked)
            pos = ", ".join(f"{p.side.value} {s} {p.unrealized(self.ex.last_price.get(s, p.entry_price)):+.2f}"
                            for s, p in self.ex.positions.items()) or "none"
            halted = " [DAILY LOSS LIMIT — paused until 00:00 UTC]" if self.core.risk.state.halted else ""
            log.info("[%s] equity $%.2f  balance $%.2f  trades %d  positions: %s  | watching: %s%s",
                     _hhmm(ts), self.ex.equity(), self.ex.balance, len(self.ex.trades), pos, active or "-", halted)

    async def run(self, feed: Feed) -> None:
        try:
            async for ev in feed.events():
                self.handle(ev)
        finally:
            self.save()
