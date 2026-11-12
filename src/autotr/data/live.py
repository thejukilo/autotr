"""Live Binance USDT-M futures market data over WebSocket (public streams, no API key).

Subscribes, per watched symbol, to `<sym>@kline_1m` (closed candles drive the strategy) and
`<sym>@bookTicker` (best bid/ask drive simulated fills, stops and targets), plus
`!markPrice@arr` for funding settlements. The watch list = top-N USDT perpetuals by 24h volume,
refreshed every `candidate_refresh_hours`, always including symbols with open positions.

Robustness: exponential-backoff reconnects, REST backfill of candles missed while disconnected,
and a watchdog that reconnects when no candle arrives for a while (also catches the 24h
connection limit and silent URL changes — the base URLs are configurable).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncIterator, Callable

import websockets

from autotr.config import Config
from autotr.data.events import Event, Funding, KlineClosed, Quote, Volumes24h, WarmupBars
from autotr.data.rest import BinanceRest, usdt_perpetuals
from autotr.models import MINUTE_MS, Bar

log = logging.getLogger(__name__)


def now_ms() -> int:
    return int(time.time() * 1000)


class BinanceFeed:
    bar_fills = False  # fills come from bookTicker quotes

    def __init__(self, cfg: Config, rest: BinanceRest, must_watch: Callable[[], set[str]] = set):
        self.cfg = cfg
        self.rest = rest
        self.must_watch = must_watch
        self.last_open: dict[str, int] = {}          # last closed 1m bar open time per symbol
        self._funding: dict[str, tuple[int, float, float]] = {}  # sym -> (next_funding_ts, rate, mark)
        self._symbols: list[str] = []

    # ------------------------------------------------------------ setup
    async def _pick_candidates(self) -> tuple[list[str], dict[str, float]]:
        """Top-N USDT perpetuals by 24h quote volume (+ symbols that must stay watched)."""
        info = await self.rest.exchange_info()
        perps = set(usdt_perpetuals(info))
        vols, _ = await self.rest.ticker_24h()
        ranked = sorted((s for s in perps if s in vols), key=lambda s: vols[s], reverse=True)
        chosen = ranked[: self.cfg.universe.live_candidates]
        for s in self.must_watch():
            if s not in chosen:
                chosen.append(s)
        return chosen, vols

    async def _warmup(self, symbols: list[str]) -> AsyncIterator[Event]:
        sem = asyncio.Semaphore(5)

        sc = self.cfg.strategy
        struct_ms = sc.structure_tf_minutes * MINUTE_MS

        async def fetch(sym: str) -> list[Bar]:
            # structure-TF history, entry-TF history, then 1m bars of the current structure bar
            async with sem:
                bars: list[Bar] = []
                if sc.structure_tf_minutes > 1:
                    bars += await self.rest.klines(sym, sc.structure_history_bars + 10,
                                                   interval_minutes=sc.structure_tf_minutes)
                if sc.entry_tf_minutes > 1:
                    bars += await self.rest.klines(sym, 500, interval_minutes=sc.entry_tf_minutes)
                if 5 not in (sc.structure_tf_minutes, sc.entry_tf_minutes):
                    bars += await self.rest.klines(sym, 100, interval_minutes=5)  # scanner volatility
                start = now_ms() - now_ms() % struct_ms
                ones = await self.rest.klines(sym, 1500, start_time=start if sc.entry_tf_minutes > 1 else None)
                return bars + ones

        new = [s for s in symbols if s not in self.last_open]
        if not new:
            return
        log.info("warming up %d symbols (%s structure + %s entry history)", len(new),
                 f"{sc.structure_history_bars}x{sc.structure_tf_minutes}m", f"{sc.entry_tf_minutes}m")
        results = await asyncio.gather(*(fetch(s) for s in new), return_exceptions=True)
        for sym, bars in zip(new, results):
            if isinstance(bars, Exception) or not bars:
                log.warning("warm-up failed for %s: %s", sym, bars)
                continue
            ones = [b for b in bars if b.interval_ms == MINUTE_MS]
            self.last_open[sym] = ones[-1].open_time if ones else now_ms() - now_ms() % MINUTE_MS - MINUTE_MS
            yield WarmupBars(bars)

    async def _backfill(self) -> AsyncIterator[Event]:
        """After a reconnect: fetch candles missed while disconnected (features only)."""
        cutoff = now_ms() - 2 * MINUTE_MS
        for sym in self._symbols:
            last = self.last_open.get(sym)
            if last is None or last >= cutoff:
                continue
            try:
                bars = await self.rest.klines(sym, 1500, start_time=last + MINUTE_MS)
            except Exception as exc:  # noqa: BLE001
                log.warning("backfill failed for %s: %s", sym, exc)
                continue
            for b in bars:
                if b.open_time > self.last_open[sym]:
                    self.last_open[sym] = b.open_time
                    yield KlineClosed(b, backfill=True)

    def _url(self) -> str:
        streams = [f"{s.lower()}@kline_1m" for s in self._symbols]
        streams += [f"{s.lower()}@bookTicker" for s in self._symbols]
        streams.append("!markPrice@arr")
        return f"{self.cfg.live.ws_url.rstrip('/')}/stream?streams=" + "/".join(streams)

    # ------------------------------------------------------------ parsing
    def _parse(self, msg: dict) -> list[Event]:
        data = msg.get("data")
        if data is None:
            return []
        if isinstance(data, list):  # !markPrice@arr
            return self._parse_mark_prices(data)
        et = data.get("e")
        if et == "kline":
            k = data["k"]
            if not k.get("x"):
                return []
            bar = Bar(k["s"], int(k["t"]), float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]),
                      float(k["v"]), float(k["q"]))
            if bar.open_time <= self.last_open.get(bar.symbol, -1):
                return []  # duplicate (e.g. already backfilled)
            self.last_open[bar.symbol] = bar.open_time
            return [KlineClosed(bar)]
        if et == "bookTicker" or ("b" in data and "a" in data and "s" in data):
            return [Quote(data["s"], float(data["b"]), float(data["a"]), int(data.get("T") or data.get("E") or now_ms()))]
        return []

    def _parse_mark_prices(self, items: list[dict]) -> list[Event]:
        out: list[Event] = []
        watched = set(self._symbols)
        for d in items:
            sym = d.get("s")
            if sym not in watched:
                continue
            nxt, rate, mark = int(d["T"]), float(d["r"]), float(d["p"])
            prev = self._funding.get(sym)
            if prev is not None and nxt > prev[0]:
                # The previous funding time has passed: it settled at the last rate we saw.
                out.append(Funding(sym, prev[1], prev[2], prev[0]))
            self._funding[sym] = (nxt, rate, mark)
        return out

    # ------------------------------------------------------------ main loop
    async def events(self) -> AsyncIterator[Event]:
        backoff = 1.0
        refresh_s = self.cfg.live.candidate_refresh_hours * 3600
        vol_every_s = self.cfg.scanner.rescan_minutes * 60
        stale_s = self.cfg.live.stream_timeout_s
        while True:
            try:
                self._symbols, _ = await self._pick_candidates()
                yield Volumes24h(*await self.rest.ticker_24h())
                async for ev in self._warmup(self._symbols):
                    yield ev
                async for ev in self._backfill():
                    yield ev
                url = self._url()
                log.info("connecting to %s (%d symbols)", self.cfg.live.ws_url, len(self._symbols))
                session_end = time.monotonic() + refresh_s
                next_vol = time.monotonic() + vol_every_s
                async with websockets.connect(url, ping_interval=20, ping_timeout=20,
                                              max_size=2**23, open_timeout=15) as ws:
                    backoff = 1.0
                    last_kline = time.monotonic()
                    while time.monotonic() < session_end:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=stale_s)
                        except asyncio.TimeoutError:
                            raw = None
                        for ev in (self._parse(json.loads(raw)) if raw else []):
                            if isinstance(ev, KlineClosed):
                                last_kline = time.monotonic()
                            yield ev
                        if time.monotonic() - last_kline > stale_s:
                            log.warning("no candles received for %.0fs — reconnecting. If this repeats, "
                                        "check live.ws_url in the config.", stale_s)
                            break
                        if time.monotonic() >= next_vol:
                            next_vol = time.monotonic() + vol_every_s
                            try:
                                yield Volumes24h(*await self.rest.ticker_24h())
                            except Exception as exc:  # noqa: BLE001
                                log.warning("24h ticker refresh failed: %s", exc)
                log.info("refreshing watch list / reconnecting")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("feed error: %s — reconnecting in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
