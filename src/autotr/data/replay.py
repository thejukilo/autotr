"""Historical market data as a time-ordered stream — shared by the backtest and the replay feed."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import AsyncIterator, Iterator

import numpy as np

from autotr.config import Config
from autotr.data.events import Event, KlineClosed, WarmupBars
from autotr.data.historical import HistoricalStore, funding_schedule
from autotr.models import Bar

log = logging.getLogger(__name__)

COLUMNS = ("open_time", "open", "high", "low", "close", "volume", "quote_volume")


def warmup_days(cfg: Config) -> int:
    """Days of history needed before trading: structure-TF history (+1 day for the 24h scanner)."""
    import math
    struct_days = cfg.strategy.structure_history_bars * cfg.strategy.structure_tf_minutes / 1440
    return max(1, math.ceil(struct_days)) + 1


def day_start_ms(d: date) -> int:
    return int(np.datetime64(d.isoformat(), "ms").astype(np.int64))


@dataclass
class MarketHistory:
    arrays: dict[str, tuple[np.ndarray, ...]]
    funding: dict[str, dict[int, float]]
    funding_source: dict[str, str]
    trade_start_ts: int
    end_ts: int

    @property
    def symbols(self) -> list[str]:
        return list(self.arrays)

    def timeline(self) -> Iterator[tuple[int, list[tuple[Bar, float | None]]]]:
        """Yield (minute_ts, [(bar, funding_rate_or_None), ...]) in time order across all symbols."""
        all_times = np.unique(np.concatenate([a[0] for a in self.arrays.values()]))
        idx = {s: 0 for s in self.arrays}
        for ts in all_times:
            ts = int(ts)
            out: list[tuple[Bar, float | None]] = []
            for sym, a in self.arrays.items():
                i = idx[sym]
                if i >= len(a[0]) or a[0][i] != ts:
                    continue
                idx[sym] = i + 1
                bar = Bar(sym, ts, float(a[1][i]), float(a[2][i]), float(a[3][i]), float(a[4][i]),
                          float(a[5][i]), float(a[6][i]))
                out.append((bar, self.funding[sym].get(ts)))
            yield ts, out

    @property
    def n_minutes(self) -> int:
        return len(np.unique(np.concatenate([a[0] for a in self.arrays.values()])))


def load_history(cfg: Config, start: date, end: date, symbols: list[str],
                 warmup: int | None = None) -> MarketHistory:
    """Load cached bars for [start - warm-up days, end] and funding events for [start, end]."""
    warmup_days_n = warmup if warmup is not None else warmup_days(cfg)
    store = HistoricalStore(cfg.data.dir)
    trade_start_ts = day_start_ms(start)
    end_ts = day_start_ms(end + timedelta(days=1))
    arrays: dict[str, tuple[np.ndarray, ...]] = {}
    funding: dict[str, dict[int, float]] = {}
    source: dict[str, str] = {}
    for sym in symbols:
        df = store.load_klines(sym, start - timedelta(days=warmup_days_n), end)
        if df.empty:
            log.warning("no data for %s — skipped (run `autotr download` first)", sym)
            continue
        arrays[sym] = tuple(df[c].to_numpy() for c in COLUMNS)
        funding[sym], source[sym] = funding_schedule(
            store, sym, start, end, trade_start_ts, end_ts, cfg.exchange.default_funding_rate)
    if not arrays:
        raise RuntimeError("no historical data found for any symbol — run `autotr download` first")
    return MarketHistory(arrays, funding, source, trade_start_ts, end_ts)


class ReplayFeed:
    """Plays history through the live engine. `speed` = simulated minutes per real second × 60
    (speed=60 → one 1m bar per second; 0 → as fast as possible)."""

    bar_fills = True  # no quotes in history: the exchange fills on bar OHLC like the backtest

    def __init__(self, history: MarketHistory, speed: float = 0.0):
        self.history = history
        self.speed = speed

    async def events(self) -> AsyncIterator[Event]:
        h = self.history
        warm: dict[str, list[Bar]] = {s: [] for s in h.symbols}
        delay = 60.0 / self.speed if self.speed > 0 else 0.0
        warmed = False
        for ts, bars in h.timeline():
            if ts < h.trade_start_ts:
                for bar, _ in bars:
                    warm[bar.symbol].append(bar)
                continue
            if not warmed:
                for sym_bars in warm.values():
                    if sym_bars:
                        yield WarmupBars(sym_bars)
                warmed = True
            for bar, rate in bars:
                yield KlineClosed(bar, funding_rate=rate)
            if delay:
                await asyncio.sleep(delay)
            else:
                await asyncio.sleep(0)  # let other tasks (e.g. Ctrl+C handling) run
