"""Grid-search strategy settings on a training period, then check the winner out-of-sample.

Ranking uses the training period only. The test period is evaluated once at the end for the
chosen settings, so its result is an honest estimate of how the settings generalise.
"""

from __future__ import annotations

import itertools
import multiprocessing as mp
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from autotr.config import Config
from autotr.data.replay import MarketHistory, load_history
from autotr.engine.backtest import run_backtest
from autotr.report.metrics import compute_metrics

# Kept deliberately small: every extra dimension makes it easier to fit noise.
DEFAULT_GRID: dict[str, list[Any]] = {
    "strategy.trend_filter": [False, True],
    "strategy.min_stop_atr5": [0.0, 0.75, 1.5],
    "strategy.take_profit_r": [1.5, 2.0, 3.0],
    "strategy.rsi_levels": [(30, 70), (25, 75)],
    "strategy.time_stop_minutes": [30, 90],
}

_HISTORY: MarketHistory | None = None  # shared with forked workers (copy-on-write)
_JOB: dict = {}


@dataclass
class RunResult:
    params: dict[str, Any]
    metrics: dict
    monthly: dict[str, float]

    @property
    def worst_month(self) -> float:
        return min(self.monthly.values()) if self.monthly else 0.0


def apply_params(cfg: Config, params: dict[str, Any]) -> Config:
    c = cfg.model_copy(deep=True)
    for key, val in params.items():
        if key.startswith("_"):
            continue  # private bookkeeping keys (e.g. ablation variant id)
        section, name = key.split(".")
        if name == "rsi_levels":
            c.strategy.rsi_oversold, c.strategy.rsi_overbought = val
            continue
        setattr(getattr(c, section), name, val)
    return c


def grid_points(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    keys = list(grid)
    return [dict(zip(keys, combo)) for combo in itertools.product(*(grid[k] for k in keys))]


def monthly_returns(trades, starting_balance: float) -> dict[str, float]:
    """Net PnL per calendar month (by exit time) as % of the balance at the start of that month."""
    by_month: dict[str, float] = defaultdict(float)
    for t in trades:
        by_month[datetime.fromtimestamp(t.exit_ts / 1000, tz=timezone.utc).strftime("%Y-%m")] += t.net_pnl
    out, bal = {}, starting_balance
    for m in sorted(by_month):
        out[m] = by_month[m] / bal * 100 if bal > 0 else 0.0
        bal += by_month[m]
    return out


def evaluate(cfg: Config, params: dict[str, Any], start: date, end: date, history: MarketHistory) -> RunResult:
    c = apply_params(cfg, params)
    res = run_backtest(c, start, end, progress=False, history=history)
    ex = res.core.exchange
    m = compute_metrics(ex.trades, res.equity, c.account.starting_balance, ex.balance, res.start_ts, res.end_ts)
    return RunResult(params, m, monthly_returns(ex.trades, c.account.starting_balance))


def _worker(params: dict[str, Any]) -> RunResult:
    global _HISTORY
    if _HISTORY is None:  # spawn start method (macOS/Windows): each worker loads once
        _HISTORY = load_history(_JOB["cfg"], _JOB["start"], _JOB["end"], _JOB["symbols"])
    return evaluate(_JOB["cfg"], params, _JOB["start"], _JOB["end"], _HISTORY)


def _init(job: dict) -> None:
    _JOB.update(job)


def run_grid(cfg: Config, start: date, end: date, symbols: list[str],
             grid: dict[str, list[Any]] | None = None, workers: int | None = None,
             on_result=None, points: list[dict[str, Any]] | None = None) -> list[RunResult]:
    """Backtest every grid combination (or the explicit `points`) in parallel worker processes."""
    global _HISTORY
    points = points if points is not None else grid_points(grid or DEFAULT_GRID)
    job = {"cfg": cfg, "start": start, "end": end, "symbols": symbols}
    ctx_name = "fork" if "fork" in mp.get_all_start_methods() else "spawn"
    if ctx_name == "fork":
        _HISTORY = load_history(cfg, start, end, symbols)
    workers = workers or max(os.cpu_count() or 1, 1)
    results: list[RunResult] = []
    with mp.get_context(ctx_name).Pool(workers, initializer=_init, initargs=(job,)) as pool:
        for r in pool.imap_unordered(_worker, points):
            results.append(r)
            if on_result:
                on_result(r, len(results), len(points))
    _HISTORY = None
    return results


def score(r: RunResult, min_trades: int) -> float:
    """Training objective: net return, but configurations with too few trades are ranked last."""
    m = r.metrics
    if m["trades"] < min_trades:
        return -1e9 + m["trades"]
    return m["return_pct"]


def describe(params: dict[str, Any]) -> str:
    parts = []
    for k, v in params.items():
        name = k.split(".")[1]
        if name == "rsi_levels":
            parts.append(f"RSI {v[0]}/{v[1]}")
        elif name == "trend_filter":
            parts.append("trend filter " + ("on" if v else "off"))
        elif name == "min_stop_atr5":
            parts.append(f"stop ≥{v}×ATR5")
        elif name == "take_profit_r":
            parts.append(f"TP {v}R")
        elif name == "time_stop_minutes":
            parts.append(f"time stop {v}m")
        else:
            parts.append(f"{name}={v}")
    return ", ".join(parts)


SCALPER_OVERRIDES: dict[str, Any] = {
    "strategy.name": "scalper", "strategy.entry_tf_minutes": 1, "strategy.structure_tf_minutes": 5,
    "strategy.structure_history_bars": 288, "scanner.rank_by": "volatility",
    "strategy.min_reward_risk_after_fees": 1.2, "strategy.max_stop_pct": 0.03,
    "strategy.time_stop_minutes": 30, "strategy.cooldown_minutes": 5,
}

ABLATION: list[tuple[str, dict[str, Any]]] = [
    ("full method", {}),
    ("no ladder (single market entry)", {"strategy.use_ladder": False}),
    ("no S/R zones", {"strategy.use_zones": False}),
    ("no channels", {"strategy.use_channels": False}),
    ("no trendlines", {"strategy.use_trendlines": False}),
    ("no Elliott", {"strategy.use_elliott": False}),
    ("scanner: volatility instead of 24h change", {"scanner.rank_by": "volatility"}),
    ("old 1m scalper", SCALPER_OVERRIDES),
]
