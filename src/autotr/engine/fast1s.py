"""PROTOTYPE: sub-minute (1-second) paper trading on Binance USDⓈ-M futures.

The normal engine decides on closed 1-minute klines. Binance does not serve 1s klines for a symbol,
but it DOES serve 1-second *continuous-contract* candles (`<pair>_perpetual@continuousKline_1s`), which
deliver a closed candle every second. This engine drives the SAME scalp_ignition strategy (confidence
sizing, laddered entries, take-profit, trailing stop) on those 1-second candles instead of minutes.

Deliberately isolated from the production 1m path (its own module + CLI command + paper account), so
it can't destabilise `autotr live`. Two differences from the 1m engine, both forced by 1s data:

* **24h context comes from the REST 24h ticker**, not from bars: a day of 1-second bars is 86 400
  candles, impractical to hold or backfill. `change_24h` (the strategy's side gate) and the mover
  ranking are taken from `/fapi/v1/ticker/24hr`, refreshed every `scanner.rescan_minutes`.
* **No REST warm-up** (REST has no 1s history): each symbol's 1-second window fills live, so a symbol
  needs ~`params.min_bars` seconds of streaming before it can trade.

Fills, stops, take-profit, trailing and liquidation are checked in real time on every bookTicker quote
(sub-second) by the shared PaperExchange — exactly as in live mode. Writes the same SQLite schema, so
`autotr web --config config/scalp_1s.yaml` shows it on the normal dashboard.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import AsyncIterator, Callable

import websockets

from autotr.config import Config
from autotr.data.events import Event, KlineClosed, Quote, Volumes24h
from autotr.data.rest import BinanceRest, usdt_perpetuals
from autotr.models import EquityPoint, Bar, Signal, Trade
from autotr.report.tradelog import LiveStore
from autotr.risk.manager import RiskManager
from autotr.sim.exchange import PaperExchange, Position
from autotr.sim.filters import FilterBook
from autotr.strategy import make_strategy

log = logging.getLogger("autotr.fast1s")

SECOND_MS = 1000
STATE_KEY = "engine"
EQUITY_EVERY_MS = 30 * 1000
SAVE_EVERY_MS = 5 * 1000
STATUS_EVERY_MS = 60 * 1000
STALE_S = 20.0            # no 1s candle for this long -> reconnect


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(slots=True)
class Watchlist:
    """The symbols eligible for NEW entries right now (the current top movers)."""
    symbols: set


class Fast1sState:
    """The minimal duck-typed state scalp_ignition needs, fed one 1-second bar at a time.

    Exposes exactly what Strategy.on_bar / manage read: bars_e, last_bar, change_24h, symbol,
    entry_ms. change_24h is injected from the 24h ticker (not computed from bars)."""

    __slots__ = ("symbol", "entry_ms", "bars_e", "last_bar", "change_24h")

    def __init__(self, symbol: str, maxlen: int):
        self.symbol = symbol
        self.entry_ms = SECOND_MS
        self.bars_e: deque[Bar] = deque(maxlen=maxlen)
        self.last_bar: Bar | None = None
        self.change_24h: float | None = None

    def add(self, bar: Bar) -> bool:
        if self.last_bar is not None and bar.open_time <= self.last_bar.open_time:
            return False  # duplicate / out of order
        self.last_bar = bar
        self.bars_e.append(bar)
        return True


class Fast1sFeed:
    """Streams 1-second continuous klines + bookTicker quotes, (re)selecting top movers each session."""

    def __init__(self, cfg: Config, rest: BinanceRest, must_watch: Callable[[], set[str]] = set):
        self.cfg = cfg
        self.rest = rest
        self.must_watch = must_watch
        self._perps: set[str] = set()

    def _urls(self, symbols: list[str]) -> dict[str, str]:
        lc = self.cfg.live
        base = lc.ws_url.rstrip("/")
        market = "/".join(f"{s.lower()}_perpetual@continuousKline_1s" for s in symbols)
        public = "/".join(f"{s.lower()}@bookTicker" for s in symbols)
        return {"market": f"{base}{lc.ws_market_path}/stream?streams={market}",
                "public": f"{base}{lc.ws_public_path}/stream?streams={public}"}

    async def _select(self) -> tuple[set[str], list[str], Volumes24h]:
        vol, chg = await self.rest.ticker_24h()
        ranked = sorted((s for s in self._perps if s in vol), key=lambda s: vol[s], reverse=True)
        pool = ranked[: self.cfg.universe.live_candidates]
        movers = sorted((s for s in pool if vol[s] >= self.cfg.scanner.min_quote_volume_24h),
                        key=lambda s: abs(chg.get(s, 0.0)), reverse=True)
        selected = set(movers[: self.cfg.scanner.top_n])
        stream = list(selected | self.must_watch())
        return selected, stream, Volumes24h(vol, chg)

    @staticmethod
    async def _reader(url: str, queue: asyncio.Queue) -> None:
        """Market (klines) reader: any failure is forwarded so the session reconnects + re-selects."""
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20,
                                          max_size=2**23, open_timeout=15) as ws:
                async for raw in ws:
                    await queue.put(raw)
            raise ConnectionError("market stream closed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await queue.put(ConnectionError(f"market stream: {exc}"))

    @staticmethod
    async def _quote_reader(url: str, queue: asyncio.Queue) -> None:
        """Quote (bookTicker) reader: self-heals quietly so a flaky quote stream can't stop trading."""
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20,
                                              max_size=2**23, open_timeout=15) as ws:
                    backoff = 1.0
                    async for raw in ws:
                        await queue.put(raw)
                raise ConnectionError("public stream closed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.debug("quote stream dropped (%s) — reconnecting in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _parse(self, raw: str) -> list[Event]:
        data = json.loads(raw).get("data")
        if not isinstance(data, dict):
            return []
        e = data.get("e")
        if e == "continuous_kline":
            k = data["k"]
            if not k.get("x"):
                return []  # candle still forming
            return [KlineClosed(Bar(data["ps"], int(k["t"]), float(k["o"]), float(k["h"]),
                                    float(k["l"]), float(k["c"]), float(k["v"]), float(k["q"]), SECOND_MS))]
        if e == "bookTicker" or ("b" in data and "a" in data and "s" in data):
            return [Quote(data["s"], float(data["b"]), float(data["a"]),
                          int(data.get("T") or data.get("E") or now_ms()))]
        return []

    async def events(self) -> AsyncIterator[Event]:
        self._perps = set(usdt_perpetuals(await self.rest.exchange_info()))
        refresh_s = self.cfg.scanner.rescan_minutes * 60
        backoff = 1.0
        while True:
            try:
                selected, stream, vols = await self._select()
                yield vols
                yield Watchlist(selected)
                urls = self._urls(stream)
                log.info("1s feed: %d candidates -> watching %d movers (%s)", len(stream), len(selected),
                         ", ".join(sorted(selected)[:8]) + ("…" if len(selected) > 8 else ""))
                queue: asyncio.Queue = asyncio.Queue(maxsize=50_000)
                readers = [asyncio.create_task(self._reader(urls["market"], queue)),
                           asyncio.create_task(self._quote_reader(urls["public"], queue))]
                try:
                    session_end = time.monotonic() + refresh_s
                    last_kline = time.monotonic()
                    while time.monotonic() < session_end:
                        try:
                            raw = await asyncio.wait_for(queue.get(), timeout=STALE_S)
                        except asyncio.TimeoutError:
                            raw = None
                        if isinstance(raw, Exception):
                            raise raw
                        for ev in (self._parse(raw) if raw else []):
                            if isinstance(ev, KlineClosed):
                                last_kline = time.monotonic()
                                backoff = 1.0
                            yield ev
                        if time.monotonic() - last_kline > STALE_S:
                            log.warning("no 1s candles for %.0fs — reconnecting", STALE_S)
                            break
                finally:
                    for t in readers:
                        t.cancel()
                    await asyncio.gather(*readers, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("1s feed error: %s — reconnecting in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)


class Fast1sEngine:
    """Drives make_strategy(cfg) on 1-second bars; reuses RiskManager + PaperExchange + LiveStore."""

    def __init__(self, cfg: Config, store: LiveStore | None, filters: FilterBook | None = None):
        self.cfg = cfg
        self.store = store
        self.ex = PaperExchange(cfg.exchange, cfg.account.starting_balance)
        self.filters = filters or FilterBook()
        self.risk = RiskManager(cfg.risk, cfg.exchange, cfg.strategy, self.filters)
        self.strat = make_strategy(cfg)
        sc, p = cfg.strategy, cfg.strategy.params
        self._maxlen = max(sc.entry_history_bars, int(p.get("min_bars", 50)),
                           int(p.get("ret_n", 12)) + 2, int(p.get("atr_n", 14)) + 2, 10)
        self.states: dict[str, Fast1sState] = {}
        self.changes: dict[str, float] = {}
        self.active: set[str] = set()
        self.last_ts: int | None = None
        self._snapshot_ms = int(cfg.live.snapshot_seconds * 1000)
        self._last_snapshot = self._next_equity = self._next_save = self._next_status = 0
        self.resumed = self._restore()
        self.ex.on_trade.append(self.risk.on_trade)
        self.ex.on_trade.append(self._on_trade)
        self.ex.on_open.append(self._on_open)

    # ------------------------------------------------------------ persistence
    def _restore(self) -> bool:
        if self.store is None:
            return False
        state = self.store.load_state(STATE_KEY)
        if not state:
            return False
        self.ex.load_state(state["exchange"])
        self.risk.load_state(state["risk"])
        self.last_ts = state.get("last_minute")
        self.filters.load_defaults_state(state.get("default_filters", {}))
        log.info("resumed paper account: balance $%.2f, %d open position(s)", self.ex.balance, len(self.ex.positions))
        return True

    def save(self) -> None:
        if self.store is not None:
            self.store.save_state(STATE_KEY, {
                "exchange": self.ex.to_state(),
                "risk": self.risk.to_state(),
                "scanner": {"active": sorted(self.active), "last_scan_ts": self.last_ts},
                "last_minute": self.last_ts,
                "default_filters": self.filters.defaults_state(),
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
        log.info("OPEN  %-5s %-12s qty %g @ %.6g  stop %.6g  tp %.6g  conf %.2f  risk $%.2f",
                 p.side.value, p.symbol, p.qty, p.entry_price, p.stop, p.take_profit, p.confidence, p.risk_usd)
        self.save()

    # ------------------------------------------------------------ events
    def handle(self, ev: Event) -> None:
        if isinstance(ev, KlineClosed):
            self._on_kline(ev.bar)
        elif isinstance(ev, Quote):
            if self.ex.busy(ev.symbol):
                self.ex.on_quote(ev.symbol, ev.bid, ev.ask, ev.ts)
            else:
                self.ex.last_price[ev.symbol] = (ev.bid + ev.ask) / 2
            if (self._snapshot_ms and self.ex.positions
                    and ev.ts - self._last_snapshot >= self._snapshot_ms):
                self._last_snapshot = ev.ts
                self.save()
        elif isinstance(ev, Volumes24h):
            self.changes = ev.change
        elif isinstance(ev, Watchlist):
            self.active = ev.symbols

    def _on_kline(self, bar: Bar) -> None:
        sym = bar.symbol
        st = self.states.get(sym)
        if st is None:
            st = self.states[sym] = Fast1sState(sym, self._maxlen)
        if not st.add(bar):
            return
        st.change_24h = self.changes.get(sym)
        self.last_ts = bar.open_time
        self.risk.update_clock(bar.close_time, self.ex.equity())

        # Manage an open position first (trail), mirroring TradingCore: a hit of a tightened stop
        # then exits as "trailing_stop". manage() also marks the symbol in-position for this candle,
        # so on_bar won't re-signal while we hold.
        pos = self.ex.positions.get(sym)
        if pos is not None:
            new = self.strat.manage(st, pos)
            s = pos.side.sign
            if new is not None and s * (new - pos.stop) > 0 and s * (bar.close - new) > 0:
                pos.stop = new
                pos.stop_trailed = True

        if sym in self.active:
            sig: Signal | None = self.strat.on_bar(st)
            if sig is not None:
                entry = self.risk.evaluate(sig, self.ex)
                if entry is not None:
                    self.ex.submit_entry(entry)

        self._periodic(bar.close_time)

    def _periodic(self, ts: int) -> None:
        if ts >= self._next_save:
            self._next_save = ts + SAVE_EVERY_MS
            self.save()
        if ts >= self._next_equity:
            self._next_equity = ts + EQUITY_EVERY_MS
            if self.store is not None:
                self.store.add_equity(EquityPoint(ts, self.ex.equity(), self.ex.balance, len(self.ex.positions)))
        if ts >= self._next_status:
            self._next_status = ts + STATUS_EVERY_MS
            pos = ", ".join(f"{p.side.value} {s} {p.unrealized(self.ex.last_price.get(s, p.entry_price)):+.2f}"
                            for s, p in self.ex.positions.items()) or "none"
            log.info("equity $%.2f  balance $%.2f  trades %d  positions: %s  | movers: %d",
                     self.ex.equity(), self.ex.balance, len(self.ex.trades), pos, len(self.active))

    async def run(self, feed: Fast1sFeed) -> None:
        try:
            async for ev in feed.events():
                self.handle(ev)
        finally:
            self.save()
