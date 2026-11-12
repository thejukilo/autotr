"""Equity curve chart."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

from autotr.models import EquityPoint, Trade  # noqa: E402


def plot_equity(equity: list[EquityPoint], trades: list[Trade], starting_balance: float,
                path: str | Path, title: str = "Equity") -> None:
    if not equity:
        return
    to_dt = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc)  # noqa: E731
    xs = [to_dt(p.ts) for p in equity]
    ys = [p.equity for p in equity]

    fig, (ax, ax2) = plt.subplots(2, 1, figsize=(11, 6.5), sharex=True,
                                  gridspec_kw={"height_ratios": [3, 1]}, constrained_layout=True)
    ax.plot(xs, ys, color="#2563eb", linewidth=1.4, label="Equity")
    ax.axhline(starting_balance, color="#9ca3af", linewidth=0.8, linestyle="--", label="Start")
    peak, dd = float("-inf"), []
    for y in ys:
        peak = max(peak, y)
        dd.append((y / peak - 1) * 100 if peak > 0 else 0)
    ax.set_ylabel("USDT")
    ax.set_title(title, loc="left", fontsize=11)
    ax.legend(loc="upper left", frameon=False, fontsize=9)
    ax.grid(alpha=0.25)

    ax2.fill_between(xs, dd, 0, color="#dc2626", alpha=0.35, linewidth=0)
    ax2.set_ylabel("Drawdown %")
    ax2.grid(alpha=0.25)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
        ax2.spines[spine].set_visible(False)
    fig.savefig(path, dpi=120)
    plt.close(fig)
