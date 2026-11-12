"""Strategy research harness: evaluate one config on fixed, named data windows.

The windows are split by purpose, to keep the search honest:

* **research**   — full diagnostics; the strategy creator learns from these.
* **validation** — summary only; used to accept/reject ideas, never to tune on trade details.
* **holdout**    — locked; only run for final candidates (`--holdout`), never used to choose ideas.

Metrics that do not depend on compounding (profit factor, average R) are the main yardsticks,
because return % compounds with 2%-of-equity risk and exaggerates both wins and losses.
"""

from __future__ import annotations

import csv
import json
import multiprocessing as mp
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from autotr.config import Config
from autotr.engine.backtest import run_backtest
from autotr.optimize import monthly_returns
from autotr.report.metrics import compute_metrics


@dataclass(frozen=True)
class Window:
    name: str
    kind: str      # research | validation | holdout
    start: date
    end: date


WINDOWS: list[Window] = [
    Window("R1", "research", date(2025, 8, 1), date(2025, 10, 31)),
    Window("R2", "research", date(2026, 1, 1), date(2026, 3, 31)),
    Window("V1", "validation", date(2025, 11, 1), date(2025, 12, 31)),
    Window("V2", "validation", date(2026, 4, 1), date(2026, 6, 30)),
    Window("H1", "holdout", date(2026, 7, 15), date(2026, 9, 15)),
    Window("H2", "holdout", date(2026, 9, 16), date(2026, 11, 10)),
]


def select_windows(kinds: set[str]) -> list[Window]:
    return [w for w in WINDOWS if w.kind in kinds]


def _hour(ts: int) -> int:
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).hour


def diagnostics(trades) -> dict:
    """Where the edge comes from (or leaks): by side, exit reason, coin, hour, fills, hold time."""
    def group(key) -> dict:
        g: dict = defaultdict(list)
        for t in trades:
            g[key(t)].append(t)
        return {str(k): {"n": len(v), "win": sum(t.net_pnl > 0 for t in v) / len(v),
                         "avg_r": sum(t.r_multiple for t in v) / len(v),
                         "net": sum(t.net_pnl for t in v)} for k, v in sorted(g.items(), key=lambda kv: str(kv[0]))}

    def hold_bucket(t) -> str:
        m = (t.exit_ts - t.entry_ts) / 60000
        return "<30m" if m < 30 else "30m-2h" if m < 120 else "2h-8h" if m < 480 else ">8h"

    coins = group(lambda t: t.symbol)
    ranked = sorted(coins.items(), key=lambda kv: kv[1]["net"])
    return {
        "by_side": group(lambda t: t.side.value),
        "by_exit": group(lambda t: t.exit_reason),
        "by_hour_utc": group(lambda t: f"{_hour(t.entry_ts) // 6 * 6:02d}-{_hour(t.entry_ts) // 6 * 6 + 6:02d}h"),
        "by_fills": group(lambda t: t.fills),
        "by_hold": group(hold_bucket),
        "worst_coins": dict(ranked[:5]),
        "best_coins": dict(ranked[-5:][::-1]),
        "gross_before_fees": sum(t.gross_pnl for t in trades),
    }


def _eval_window(job: tuple[Config, Window, str | None]) -> dict:
    cfg, w, trades_dir = job
    res = run_backtest(cfg, w.start, w.end, progress=False)
    ex = res.core.exchange
    m = compute_metrics(ex.trades, res.equity, cfg.account.starting_balance, ex.balance, res.start_ts, res.end_ts)
    out = {
        "window": w.name, "kind": w.kind, "start": str(w.start), "end": str(w.end),
        "return_pct": m["return_pct"], "trades": m["trades"], "trades_per_day": m["trades_per_day"],
        "win_rate": m["win_rate"], "profit_factor": m["profit_factor"], "avg_r": m["expectancy_r"],
        "max_drawdown_pct": m["max_drawdown_pct"], "fees": m["fees"], "net_pnl": m["net_pnl"],
        "avg_hold_min": m["avg_hold_min"], "monthly": monthly_returns(ex.trades, cfg.account.starting_balance),
        "signals": len(res.core.signals),
    }
    if w.kind == "research":
        out["diagnostics"] = diagnostics(ex.trades)
        if trades_dir:
            p = Path(trades_dir) / f"trades_{w.name}.csv"
            with open(p, "w", newline="") as fh:
                wr = csv.writer(fh)
                wr.writerow(["symbol", "side", "entry_time", "exit_time", "entry", "exit", "stop", "tp",
                             "exit_reason", "r", "net_pnl", "fills", "reason"])
                for t in ex.trades:
                    wr.writerow([t.symbol, t.side.value,
                                 datetime.fromtimestamp(t.entry_ts / 1000, tz=timezone.utc).isoformat(),
                                 datetime.fromtimestamp(t.exit_ts / 1000, tz=timezone.utc).isoformat(),
                                 f"{t.entry_price:.6g}", f"{t.exit_price:.6g}", f"{t.stop:.6g}",
                                 f"{t.take_profit:.6g}", t.exit_reason, f"{t.r_multiple:.3f}",
                                 f"{t.net_pnl:.4f}", t.fills, t.entry_reason])
    return out


PROGRESS_LOG = Path("reports/research/progress.log")


def log_progress(msg: str) -> None:
    """Print and append to the shared progress log (`tail -f reports/research/progress.log`)."""
    line = f"{datetime.now().strftime('%H:%M:%S')}  {msg}"
    print(line, flush=True)
    PROGRESS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(PROGRESS_LOG, "a") as fh:
        fh.write(line + "\n")


def evaluate_windows(cfg: Config, windows: list[Window], out_dir: Path, workers: int) -> list[dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    jobs = [(cfg, w, str(out_dir)) for w in windows]
    ctx = mp.get_context("fork" if "fork" in mp.get_all_start_methods() else "spawn")
    name = out_dir.name
    log_progress(f"{name}: START {len(jobs)} windows ({', '.join(w.name for w in windows)})")
    t0 = time.time()
    results = []
    with ctx.Pool(max(1, min(workers, len(jobs)))) as pool:
        for r in pool.imap_unordered(_eval_window, jobs):
            results.append(r)
            log_progress(f"{name}: [{len(results)}/{len(jobs)} {time.time() - t0:4.0f}s] {r['window']} {r['kind']:10} "
                         f"{r['return_pct']:+7.1f}%  {r['trades']:4d} trades  win {r['win_rate']:.0%}  "
                         f"PF {r['profit_factor']:.2f}  avg R {r['avg_r']:+.3f}")
    log_progress(f"{name}: DONE in {time.time() - t0:.0f}s")
    order = {w.name: i for i, w in enumerate(WINDOWS)}
    results.sort(key=lambda r: order[r["window"]])
    return results


def aggregate(results: list[dict], kind: str) -> dict:
    rs = [r for r in results if r["kind"] == kind]
    if not rs:
        return {}
    n = sum(r["trades"] for r in rs)
    return {
        "trades": n,
        "avg_r": sum(r["avg_r"] * r["trades"] for r in rs) / n if n else 0.0,
        "windows_positive": sum(r["return_pct"] > 0 for r in rs),
        "windows": len(rs),
        "min_pf": min(r["profit_factor"] for r in rs),
        "returns": [round(r["return_pct"], 1) for r in rs],
    }


def summary_table(name: str, results: list[dict]) -> str:
    lines = [f"{name}",
             f"  {'win':3} {'kind':10} {'period':23} {'return':>8} {'trades':>6} {'/day':>5} {'win%':>5} "
             f"{'PF':>5} {'avg R':>7} {'maxDD':>6} {'fees':>6}"]
    for r in results:
        lines.append(f"  {r['window']:3} {r['kind']:10} {r['start']}..{r['end']} {r['return_pct']:+7.1f}% "
                     f"{r['trades']:6d} {r['trades_per_day']:5.1f} {r['win_rate']:5.0%} {r['profit_factor']:5.2f} "
                     f"{r['avg_r']:+7.3f} {r['max_drawdown_pct']:5.1f}% {r['fees']:6.2f}")
    for kind in ("research", "validation", "holdout"):
        a = aggregate(results, kind)
        if a:
            lines.append(f"  {kind:10} total: {a['trades']} trades, avg R {a['avg_r']:+.3f}, "
                         f"{a['windows_positive']}/{a['windows']} windows positive, min PF {a['min_pf']:.2f}")
    return "\n".join(lines)


def save(out_dir: Path, name: str, config_path: str, results: list[dict]) -> None:
    (out_dir / "results.json").write_text(json.dumps(
        {"strategy": name, "config": config_path, "results": results,
         "aggregate": {k: aggregate(results, k) for k in ("research", "validation", "holdout")}},
        indent=2, default=str))
    (out_dir / "summary.txt").write_text(summary_table(name, results) + "\n")
