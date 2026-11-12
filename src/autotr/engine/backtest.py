"""Backtest engine: replays cached 1m history for many symbols in timestamp order."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np

from autotr.config import Config
from autotr.data.historical import HistoricalStore
from autotr.engine.core import TradingCore
from autotr.models import HOUR_MS, MINUTE_MS, Bar, EquityPoint
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


def _funding_schedule(store: HistoricalStore, symbol: str, start: date, end: date,
                      start_ts: int, end_ts: int, default_rate: float) -> tuple[dict[int, float], str]:
    df = store.load_funding(symbol, start, end)
    events: dict[int, float] = {}
    covered_until = start_ts
    if len(df):
        for t, r in zip(df["time"].to_numpy(), df["rate"].to_numpy()):
            if start_ts <= t <= end_ts:
                events[int(t)] = float(r)
        covered_until = int(df["time"].max())
    # Months not yet published: assume the default rate every 8h (00/08/16 UTC).
    t = (max(covered_until, start_ts) // (8 * HOUR_MS) + 1) * 8 * HOUR_MS
    filled = 0
    while t <= end_ts:
        events.setdefault(t, default_rate)
        t += 8 * HOUR_MS
        filled += 1
    source = "historical" if filled == 0 else ("default" if not len(df) else "historical+default")
    return events, source


def run_backtest(cfg: Config, start: date, end: date, symbols: list[str] | None = None,
                 warmup_days: int = 1, progress: bool = True) -> BacktestResult:
    """Run over [start, end] (inclusive UTC days). `warmup_days` before `start` feed indicators
    and the scanner's 24h volume without trading."""
    store = HistoricalStore(cfg.data.dir)
    symbols = symbols or cfg.universe.symbols
    load_start = start - timedelta(days=warmup_days)
    filters = FilterBook.load(cfg.data.dir)
    core = TradingCore(cfg, filters)
    ex = core.exchange

    trade_start_ts = int(np.datetime64(start.isoformat(), "ms").astype(np.int64))
    end_ts = int(np.datetime64((end + timedelta(days=1)).isoformat(), "ms").astype(np.int64))

    # ---- load data
    arrays: dict[str, tuple[np.ndarray, ...]] = {}
    funding: dict[str, dict[int, float]] = {}
    funding_source: dict[str, str] = {}
    for sym in symbols:
        df = store.load_klines(sym, load_start, end)
        if df.empty:
            log.warning("no data for %s — skipped (run `autotr download` first)", sym)
            continue
        arrays[sym] = tuple(df[c].to_numpy() for c in
                            ("open_time", "open", "high", "low", "close", "volume", "quote_volume"))
        funding[sym], funding_source[sym] = _funding_schedule(
            store, sym, start, end, trade_start_ts, end_ts, cfg.exchange.default_funding_rate)
    if not arrays:
        raise RuntimeError("no historical data found for any symbol")

    # ---- build a global minute timeline: for every minute, which (symbol, row) bars exist
    all_times = np.unique(np.concatenate([a[0] for a in arrays.values()]))
    idx = {s: 0 for s in arrays}
    equity: list[EquityPoint] = []
    next_sample = trade_start_ts
    t0 = time.perf_counter()
    total = len(all_times)
    report_every = max(total // 20, 1)

    for n, ts in enumerate(all_times):
        ts = int(ts)
        trading = ts >= trade_start_ts
        for sym, a in arrays.items():
            i = idx[sym]
            if i >= len(a[0]) or a[0][i] != ts:
                continue
            idx[sym] = i + 1
            bar = Bar(sym, ts, float(a[1][i]), float(a[2][i]), float(a[3][i]), float(a[4][i]),
                      float(a[5][i]), float(a[6][i]))
            if not trading:
                core.warmup_bar(bar)
                continue
            ex.on_bar(bar)
            rate = funding[sym].get(ts)
            if rate is not None:
                ex.on_funding(sym, rate, bar.open, ts)
            core.on_bar_closed(bar)
        if trading:
            core.maybe_rescan(ts + MINUTE_MS)
            if ts >= next_sample:
                equity.append(EquityPoint(ts, ex.equity(), ex.balance, len(ex.positions)))
                next_sample += EQUITY_SAMPLE_MS
        if progress and n % report_every == 0 and n:
            log.info("backtest %3.0f%%  trades=%d  equity=%.2f", 100 * n / total, len(ex.trades), ex.equity())

    # Close anything still open at the last price so the result is fully realised.
    last_ts = int(all_times[-1]) + MINUTE_MS
    for sym in list(ex.positions):
        ex.close_market(sym, ex.last_price[sym], last_ts, "end_of_backtest")
    ex.pending.clear()
    equity.append(EquityPoint(last_ts, ex.equity(), ex.balance, 0))

    return BacktestResult(core, equity, trade_start_ts, last_ts, list(arrays), time.perf_counter() - t0,
                          funding_source)
