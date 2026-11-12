"""Integration: the live engine fed by a replay must trade exactly like the backtest."""

import asyncio
from datetime import date

import numpy as np
import pandas as pd
import pytest

from autotr.config import load_config
from autotr.data.historical import HistoricalStore
from autotr.data.replay import ReplayFeed, load_history
from autotr.engine.backtest import run_backtest
from autotr.engine.live import LiveEngine
from autotr.models import DAY_MS, MINUTE_MS
from autotr.report.tradelog import LiveStore

SYMBOLS = ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
DAYS = [date(2026, 1, d) for d in range(1, 9)]   # 8 days: confluence needs days of 1h structure


def _write_synthetic(data_dir):
    store = HistoricalStore(data_dir)
    rng = np.random.default_rng(7)
    for k, sym in enumerate(SYMBOLS):
        n = 1440 * len(DAYS)
        # mean-reverting oscillation + noise: produces RSI extremes and repeated swing levels
        t = np.arange(n)
        base = 100 * (1 + 0.02 * np.sin(t / (90 + 30 * k)) + 0.01 * np.sin(t / 17))
        close = base * np.exp(np.cumsum(rng.normal(0, 0.0008, n)) * 0.3)
        open_ = np.r_[close[0], close[:-1]]
        spread = np.abs(rng.normal(0, 0.0012, n)) * close
        high = np.maximum(open_, close) + spread
        low = np.minimum(open_, close) - spread
        vol = rng.uniform(5_000, 10_000, n)
        start_ms = int(np.datetime64(DAYS[0].isoformat(), "ms").astype(np.int64))
        df = pd.DataFrame({"open_time": start_ms + t * MINUTE_MS, "open": open_, "high": high, "low": low,
                           "close": close, "volume": vol, "quote_volume": vol * close})
        for i, d in enumerate(DAYS):
            p = store.kline_path(sym, d)
            p.parent.mkdir(parents=True, exist_ok=True)
            df.iloc[i * 1440:(i + 1) * 1440].to_parquet(p, index=False)


@pytest.fixture(params=["scalper", "confluence"])
def cfg(request, tmp_path):
    _write_synthetic(tmp_path / "data")
    c = load_config("config/scalper.yaml") if request.param == "scalper" else load_config()
    if request.param == "confluence":
        c.strategy.structure_history_bars = 96   # 4 days of 1h: fits the synthetic data
        c.strategy.min_score = 2.5               # synthetic data: make sure some trades happen
    c.data.dir = str(tmp_path / "data")
    c.universe.symbols = SYMBOLS
    c.scanner.min_quote_volume_24h = 0
    c.scanner.top_n = 3
    c.risk.daily_loss_limit = 1.0  # don't let the guard hide differences
    return c


def test_replay_matches_backtest(cfg, tmp_path):
    start, end = DAYS[5], DAYS[-1]
    bt = run_backtest(cfg, start, end, SYMBOLS, progress=False)
    bt_trades = [t for t in bt.core.exchange.trades if t.exit_reason != "end_of_backtest"]
    assert len(bt_trades) >= 3, "synthetic data should produce some trades"

    store = LiveStore(tmp_path / "replay.db")
    engine = LiveEngine(cfg, store, bar_fills=True)
    asyncio.run(engine.run(ReplayFeed(load_history(cfg, start, end, SYMBOLS))))
    live_trades = engine.ex.trades

    key = lambda t: (t.symbol, t.side, t.entry_ts, t.exit_ts, round(t.qty, 9), round(t.net_pnl, 9), t.exit_reason)
    assert [key(t) for t in live_trades] == [key(t) for t in bt_trades]
    # persisted trade log matches too
    assert [key(t) for t in store.trades()] == [key(t) for t in live_trades]
    store.close()


def test_live_state_survives_restart(cfg, tmp_path):
    start, end = DAYS[5], DAYS[-1]
    h = load_history(cfg, start, end, SYMBOLS)
    db = tmp_path / "live.db"

    # Run the whole period in one go
    full = LiveEngine(cfg, LiveStore(tmp_path / "full.db"), bar_fills=True)
    asyncio.run(full.run(ReplayFeed(h)))

    # Run until the middle, "crash", restart from the database, continue with the rest
    async def run_until(engine, stop_ts=None, skip_before=None):
        async for ev in ReplayFeed(h).events():
            bar = getattr(ev, "bar", None)
            if bar is not None and stop_ts is not None and bar.open_time >= stop_ts:
                break
            if bar is not None and skip_before is not None and bar.open_time < skip_before:
                engine.core.warmup_bar(bar)  # restarted bot re-warms from exchange history
                engine.last_seen[bar.symbol] = bar.open_time
                continue
            engine.handle(ev)
        engine.save()

    mid = h.trade_start_ts + DAY_MS + 7 * 60 * MINUTE_MS
    first = LiveEngine(cfg, LiveStore(db), bar_fills=True)
    asyncio.run(run_until(first, stop_ts=mid))
    n_first = len(first.ex.trades)
    second = LiveEngine(cfg, LiveStore(db), bar_fills=True)
    assert second.resumed
    assert second.ex.balance == pytest.approx(first.ex.balance)
    assert set(second.ex.positions) == set(first.ex.positions)
    asyncio.run(run_until(second, skip_before=mid))
    # trades and final balance should match the uninterrupted run
    stored = LiveStore(db).trades()
    assert len(stored) == len(full.ex.trades) and n_first <= len(stored)
    assert second.ex.balance == pytest.approx(full.ex.balance, abs=1e-9)
