"""Performance metrics from a list of trades and an equity curve."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Iterable

from autotr.models import DAY_MS, MINUTE_MS, EquityPoint, Trade


def max_drawdown(values: Iterable[float]) -> float:
    """Largest peak-to-trough decline as a fraction of the peak (0.25 = -25%)."""
    peak, mdd = float("-inf"), 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    return mdd


def compute_metrics(trades: list[Trade], equity: list[EquityPoint], starting_balance: float,
                    final_balance: float, start_ts: int, end_ts: int) -> dict:
    n = len(trades)
    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl <= 0]
    gross = sum(t.gross_pnl for t in trades)
    fees = sum(t.fees for t in trades)
    funding = sum(t.funding for t in trades)
    net = sum(t.net_pnl for t in trades)
    sum_win = sum(t.net_pnl for t in wins)
    sum_loss = -sum(t.net_pnl for t in losses)
    days = max((end_ts - start_ts) / DAY_MS, 1e-9)

    # Drawdown on the sampled equity curve plus the realised balance after each trade.
    balance_path = [starting_balance]
    for t in sorted(trades, key=lambda t: t.exit_ts):
        balance_path.append(balance_path[-1] + t.net_pnl)
    mdd = max(max_drawdown(p.equity for p in equity) if equity else 0.0, max_drawdown(balance_path))

    per_symbol: dict[str, dict] = defaultdict(lambda: {"trades": 0, "net_pnl": 0.0, "wins": 0})
    for t in trades:
        row = per_symbol[t.symbol]
        row["trades"] += 1
        row["net_pnl"] += t.net_pnl
        row["wins"] += t.net_pnl > 0
    for row in per_symbol.values():
        row["win_rate"] = row["wins"] / row["trades"]

    by_side = {}
    for side in ("LONG", "SHORT"):
        ts_ = [t for t in trades if t.side.value == side]
        by_side[side] = {
            "trades": len(ts_),
            "win_rate": (sum(t.net_pnl > 0 for t in ts_) / len(ts_)) if ts_ else 0.0,
            "net_pnl": sum(t.net_pnl for t in ts_),
        }

    return {
        "period_days": round(days, 2),
        "starting_balance": starting_balance,
        "final_balance": final_balance,
        "return_pct": (final_balance / starting_balance - 1) * 100,
        "trades": n,
        "trades_per_day": n / days,
        "win_rate": len(wins) / n if n else 0.0,
        "gross_pnl": gross,
        "fees": fees,
        "funding": funding,
        "net_pnl": net,
        "profit_factor": (sum_win / sum_loss) if sum_loss > 0 else (float("inf") if sum_win > 0 else 0.0),
        "avg_win": sum_win / len(wins) if wins else 0.0,
        "avg_loss": -sum_loss / len(losses) if losses else 0.0,
        "expectancy_r": sum(t.r_multiple for t in trades) / n if n else 0.0,
        "avg_risk_usd": sum(t.risk_usd for t in trades) / n if n else 0.0,
        "avg_hold_min": sum(t.exit_ts - t.entry_ts for t in trades) / n / MINUTE_MS if n else 0.0,
        "max_drawdown_pct": mdd * 100,
        "fees_as_pct_of_gross_abs": (fees / sum(abs(t.gross_pnl) for t in trades) * 100) if n else 0.0,
        "exit_reasons": dict(Counter(t.exit_reason for t in trades)),
        "by_side": by_side,
        "per_symbol": dict(sorted(per_symbol.items(), key=lambda kv: kv[1]["net_pnl"], reverse=True)),
        # Σ net trade PnL must equal the change in wallet balance — a sanity check of the accounting.
        "reconciliation_error": round(starting_balance + net - final_balance, 8),
    }


def format_summary(m: dict) -> str:
    lines = [
        f"Period            {m['period_days']:.1f} days",
        f"Balance           ${m['starting_balance']:.2f} -> ${m['final_balance']:.2f}  ({m['return_pct']:+.2f}%)",
        f"Trades            {m['trades']}  ({m['trades_per_day']:.1f}/day)",
        f"Win rate          {m['win_rate']:.1%}",
        f"Profit factor     {m['profit_factor']:.2f}",
        f"Expectancy        {m['expectancy_r']:+.3f} R per trade (avg risk ${m['avg_risk_usd']:.2f})",
        f"Avg win / loss    ${m['avg_win']:.3f} / ${m['avg_loss']:.3f}",
        f"Gross PnL         ${m['gross_pnl']:+.2f}",
        f"Fees              ${-m['fees']:+.2f}",
        f"Funding           ${m['funding']:+.2f}",
        f"Net PnL           ${m['net_pnl']:+.2f}",
        f"Max drawdown      {m['max_drawdown_pct']:.1f}%",
        f"Avg hold          {m['avg_hold_min']:.1f} min",
        f"Exit reasons      {m['exit_reasons']}",
        "Long / short      " + ", ".join(
            f"{k}: {v['trades']} trades, {v['win_rate']:.0%} win, ${v['net_pnl']:+.2f}" for k, v in m["by_side"].items()),
    ]
    top = list(m["per_symbol"].items())
    if top:
        lines.append("Per symbol        " + ", ".join(f"{s} {r['trades']}t ${r['net_pnl']:+.2f}" for s, r in top[:6])
                     + (" ..." if len(top) > 6 else ""))
    if abs(m["reconciliation_error"]) > 1e-6:
        lines.append(f"!! accounting mismatch: {m['reconciliation_error']}")
    return "\n".join(lines)
