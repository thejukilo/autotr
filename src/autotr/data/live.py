"""Live Binance USDT-M futures market data over WebSocket (public streams, no API key).

Subscribes, per watched symbol, to `<sym>@kline_1m` (closed candles drive the strategy) and
`<sym>@bookTicker` (best bid/ask drive simulated fills, stops and targets), plus
`!markPrice@arr` for funding settlements. Binance serves these on separate endpoints, so there are
two connections: `{ws_url}{ws_market_path}` (klines + mark prices) and `{ws_url}{ws_public_path}`
(bookTicker). The legacy root URL no longer delivers klines. The watch list = top-N USDT perpetuals by 24h volume,
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
        if self.cfg.universe.live_use_symbols:
            missing = [x for x in self.cfg.universe.symbols if x not in perps]
            if missing:
                log.warning("not tradable on Binance (skipped): %s", ", ".join(missing))
            chosen = [x for x in self.cfg.universe.symbols if x in perps]
        else:
            chosen = ranked[: self.cfg.universe.live_candidates]
        for s in self.must_watch():
            if s not in chosen:
                chosen.append(s)
        return chosen, vols

    async def _warmup(self, symbols: list[str]) -> AsyncIterator[Event]:
        sem = asyncio.Semaphore(5)

        sc = self.cfg.strategy

        async def fetch(sym: str) -> list[Bar]:
            # structure-TF history, entry-TF history, then ~25h of 1m bars. The day of 1m bars is
            # what feeds change_24h (needs a full 24h of 1m history); without it every strategy that
            # gates on change_24h (the scalp_* family) cannot trade until 24h after start-up.
            async with sem:
                bars: list[Bar] = []
                if sc.structure_tf_minutes > 1:
                    bars += await self.rest.klines(sym, sc.structure_history_bars + 10,
                                                   interval_minutes=sc.structure_tf_minutes)
                if sc.entry_tf_minutes > 1:
                    bars += await self.rest.klines(sym, 500, interval_minutes=sc.entry_tf_minutes)
                if 5 not in (sc.structure_tf_minutes, sc.entry_tf_minutes):
                    bars += await self.rest.klines(sym, 100, interval_minutes=5)  # scanner volatility
                ones = await self.rest.klines(sym, 1500, interval_minutes=1)  # latest ~25h of 1m bars
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

    def _urls(self) -> dict[str, str]:
        """Combined-stream URL per endpoint: klines + mark prices on `market`, quotes on `public`."""
        lc = self.cfg.live
        base = lc.ws_url.rstrip("/")
        market = [f"{s.lower()}@kline_1m" for s in self._symbols] + ["!markPrice@arr"]
        public = [f"{s.lower()}@bookTicker" for s in self._symbols]
        return {
            "market": f"{base}{lc.ws_market_path}/stream?streams=" + "/".join(market),
            "public": f"{base}{lc.ws_public_path}/stream?streams=" + "/".join(public),
        }

    @staticmethod
    async def _reader(name: str, url: str, queue: asyncio.Queue) -> None:
        """Pump one WebSocket into the shared queue; any failure is forwarded so the loop reconnects."""
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20,
                                          max_size=2**23, open_timeout=15) as ws:
                async for raw in ws:
                    await queue.put(raw)
            raise ConnectionError(f"{name} stream closed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await queue.put(ConnectionError(f"{name} stream: {exc}"))

    async def _quote_reader(self, url: str, queue: asyncio.Queue) -> None:
        """Self-healing pump for the quote (bookTicker) stream. Binance's public endpoint drops
        repeatedly under load; reconnect it in the background (gentle backoff, quiet logging) instead
        of forwarding the failure, so a flaky quote stream never tears down the klines stream."""
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
                urls = self._urls()
                log.info("connecting to %s (%s + %s, %d symbols)", self.cfg.live.ws_url,
                         self.cfg.live.ws_market_path, self.cfg.live.ws_public_path, len(self._symbols))
                session_end = time.monotonic() + refresh_s
                next_vol = time.monotonic() + vol_every_s
                queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)
                # The market stream (klines) stays fatal: a drop triggers a full reconnect + backfill.
                # The quote stream self-heals in the background, so its frequent drops never stop trading.
                readers = [asyncio.create_task(self._reader("market", urls["market"], queue)),
                           asyncio.create_task(self._quote_reader(urls["public"], queue))]
                try:
                    last_kline = time.monotonic()
                    while time.monotonic() < session_end:
                        try:
                            raw = await asyncio.wait_for(queue.get(), timeout=stale_s)
                        except asyncio.TimeoutError:
                            raw = None
                        if isinstance(raw, Exception):
                            raise raw
                        for ev in (self._parse(json.loads(raw)) if raw else []):
                            if isinstance(ev, KlineClosed):
                                last_kline = time.monotonic()
                                backoff = 1.0
                            yield ev
                        if time.monotonic() - last_kline > stale_s:
                            log.warning("no candles received for %.0fs — reconnecting. If this repeats, "
                                        "check live.ws_url / live.ws_market_path in the config.", stale_s)
                            break
                        if time.monotonic() >= next_vol:
                            next_vol = time.monotonic() + vol_every_s
                            try:
                                yield Volumes24h(*await self.rest.ticker_24h())
                            except Exception as exc:  # noqa: BLE001
                                log.warning("24h ticker refresh failed: %s", exc)
                finally:
                    for t in readers:
                        t.cancel()
                    await asyncio.gather(*readers, return_exceptions=True)
                log.info("refreshing watch list / reconnecting")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("feed error: %s — reconnecting in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
