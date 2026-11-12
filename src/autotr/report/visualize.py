"""Backtest visualisations: a performance dashboard and a gallery of individual trades.

The trade gallery re-creates what the bot "saw" at entry time — S/R zones, trendlines and RSI —
by replaying the symbol's candles through a fresh SymbolState up to the signal bar.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

from autotr.config import Config  # noqa: E402
from autotr.data.historical import HistoricalStore  # noqa: E402
from autotr.indicators.rsi import rsi as rsi_series  # noqa: E402
from autotr.market.features import SymbolState  # noqa: E402
from autotr.models import DAY_MS, MINUTE_MS, Bar, EquityPoint, Side, Trade  # noqa: E402

# Reference palette (dataviz skill, light mode)
SURFACE = "#fcfcfb"
GRID = "#e8e7e2"
TEXT = "#0b0b0b"
TEXT_2 = "#52514e"
MUTED = "#8a8984"
BLUE = "#2a78d6"      # profit / take-profit (diverging pole)
RED = "#e34948"       # loss / stop (diverging pole)
VIOLET = "#6250d6"    # trendline
AQUA = "#1baf7a"      # channel
ORANGE = "#eb6834"    # Elliott wave count
ZONE = "#d9d7cf"      # S/R zone band (neutral)
CANDLE_UP = "#c3c2b7"
CANDLE_DOWN = "#52514e"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": GRID, "axes.labelcolor": TEXT_2, "xtick.color": TEXT_2, "ytick.color": TEXT_2,
    "text.color": TEXT, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
    "axes.spines.top": False, "axes.spines.right": False, "font.size": 9,
    "axes.titlesize": 10, "axes.titleweight": "bold", "axes.titlelocation": "left",
    "legend.frameon": False, "legend.fontsize": 8,
})


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _bars(ax, xs, values, colors, horizontal=False, width=0.6):
    for x, v, c in zip(xs, values, colors):
        if horizontal:
            ax.barh(x, v, height=width, color=c, linewidth=0)
        else:
            ax.bar(x, v, width=width, color=c, linewidth=0)


# ------------------------------------------------------------------ dashboard
def plot_dashboard(trades: list[Trade], equity: list[EquityPoint], metrics: dict,
                   path: str | Path, title: str) -> None:
    fig = plt.figure(figsize=(13, 9.2), constrained_layout=True)
    gs = fig.add_gridspec(3, 3, height_ratios=[2.2, 0.8, 2.0])
    m = metrics

    kpi = (f"${m['starting_balance']:.0f} → ${m['final_balance']:.2f} ({m['return_pct']:+.1f}%)   ·   "
           f"{m['trades']} trades, {m['win_rate']:.0%} win rate   ·   profit factor {m['profit_factor']:.2f}   ·   "
           f"max drawdown {m['max_drawdown_pct']:.1f}%   ·   fees ${m['fees']:.2f}")
    fig.suptitle(f"{title}\n", x=0.01, ha="left", fontsize=13, fontweight="bold")
    fig.text(0.01, 0.955, kpi, ha="left", fontsize=10, color=TEXT_2)

    # --- equity
    ax = fig.add_subplot(gs[0, :])
    xs = [_dt(p.ts) for p in equity]
    ys = [p.equity for p in equity]
    start = m["starting_balance"]
    ax.plot(xs, ys, color=BLUE, linewidth=2, solid_joinstyle="round")
    ax.fill_between(xs, ys, start, where=[y >= start for y in ys], color=BLUE, alpha=0.10, linewidth=0)
    ax.fill_between(xs, ys, start, where=[y < start for y in ys], color=RED, alpha=0.10, linewidth=0)
    ax.axhline(start, color=MUTED, linewidth=1)
    ax.annotate(f"start ${start:.0f}", (xs[0], start), xytext=(4, 4), textcoords="offset points",
                color=TEXT_2, fontsize=8)
    ax.plot([xs[-1]], [ys[-1]], "o", color=BLUE, markersize=7, markeredgecolor=SURFACE, markeredgewidth=2)
    ax.annotate(f"${ys[-1]:.2f}", (xs[-1], ys[-1]), xytext=(6, -3), textcoords="offset points",
                color=TEXT, fontsize=9, fontweight="bold")
    ax.set_title("Account equity (USDT)")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))

    # --- drawdown
    ax = fig.add_subplot(gs[1, :], sharex=ax)
    peak, dd = -np.inf, []
    for y in ys:
        peak = max(peak, y)
        dd.append((y / peak - 1) * 100)
    ax.fill_between(xs, dd, 0, color=RED, alpha=0.18, linewidth=0)
    ax.plot(xs, dd, color=RED, linewidth=1.2)
    ax.set_title("Drawdown from peak (%)")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))

    # --- R-multiple distribution
    ax = fig.add_subplot(gs[2, 0])
    r = np.array([t.r_multiple for t in trades])
    bins = np.arange(np.floor(r.min() * 4) / 4 - 0.25, np.ceil(r.max() * 4) / 4 + 0.5, 0.25) if len(r) else [0, 1]
    counts, edges = np.histogram(r, bins=bins)
    centers = (edges[:-1] + edges[1:]) / 2
    _bars(ax, centers, counts, [BLUE if c > 0 else RED for c in centers], width=0.21)
    ax.axvline(0, color=MUTED, linewidth=1)
    ax.axvline(r.mean() if len(r) else 0, color=TEXT, linewidth=1.2, linestyle=(0, (3, 2)))
    ax.annotate(f"average {r.mean():+.2f}R" if len(r) else "", (r.mean() if len(r) else 0, counts.max() if len(counts) else 0),
                xytext=(5, -2), textcoords="offset points", fontsize=8, color=TEXT)
    ax.set_title("Result per trade (R = multiples of risk)")
    ax.set_xlabel("R after fees")
    ax.set_ylabel("trades")
    ax.legend(handles=[Patch(color=BLUE, label="profit"), Patch(color=RED, label="loss")], loc="upper right")

    # --- where the money went
    ax = fig.add_subplot(gs[2, 1])
    labels = ["Gross\ntrading", "Fees", "Funding", "Net"]
    vals = [m["gross_pnl"], -m["fees"], m["funding"], m["net_pnl"]]
    xs_b = np.arange(len(vals))
    _bars(ax, xs_b, vals, [BLUE if v >= 0 else RED for v in vals], width=0.55)
    for x, v in zip(xs_b, vals):
        ax.annotate(f"${v:+.2f}", (x, v), xytext=(0, 4 if v >= 0 else -12), textcoords="offset points",
                    ha="center", fontsize=9, color=TEXT)
    ax.axhline(0, color=MUTED, linewidth=1)
    ax.set_xticks(xs_b, labels)
    ax.set_title("Where the money went (USDT)")
    ax.grid(axis="x", visible=False)
    lo, hi = min(vals + [0]), max(vals + [0])
    ax.set_ylim(lo - (hi - lo) * 0.15, hi + (hi - lo) * 0.15 + 0.5)

    # --- per symbol
    ax = fig.add_subplot(gs[2, 2])
    per = sorted(m["per_symbol"].items(), key=lambda kv: kv[1]["net_pnl"])
    if len(per) > 14:
        per = per[:7] + per[-7:]
    names = [s.replace("USDT", "") for s, _ in per]
    vals = [row["net_pnl"] for _, row in per]
    ys_b = np.arange(len(per))
    _bars(ax, ys_b, vals, [BLUE if v >= 0 else RED for v in vals], horizontal=True, width=0.6)
    ax.set_yticks(ys_b, [f"{n} ({row['trades']})" for n, (_, row) in zip(names, per)])
    ax.axvline(0, color=MUTED, linewidth=1)
    ax.grid(axis="y", visible=False)
    ax.set_title("Net result per coin (USDT, trade count)")

    fig.savefig(path, dpi=120)
    plt.close(fig)


# ------------------------------------------------------------------ trade gallery
def _pick_trades(trades: list[Trade], n_each: int = 3) -> list[Trade]:
    wins = sorted([t for t in trades if t.exit_reason == "take_profit"], key=lambda t: -t.net_pnl)[:n_each]
    stops = [t for t in trades if t.exit_reason == "stop_loss"]
    stops = sorted(stops, key=lambda t: abs(t.r_multiple + 1.0))[:n_each]  # typical, ~-1R losses
    return wins + stops


def _bars_df(store: HistoricalStore, symbol: str, t_from: int, t_to: int):
    d0, d1 = _dt(t_from).date(), _dt(t_to).date()
    df = store.load_klines(symbol, d0, d1)
    return df[(df.open_time >= t_from) & (df.open_time < t_to)].reset_index(drop=True)


def _resample(df, tf_ms: int):
    """Aggregate 1m rows into `tf_ms` candles (UTC-aligned)."""
    if tf_ms == MINUTE_MS or df.empty:
        return df.reset_index(drop=True)
    g = df.groupby(df.open_time - df.open_time % tf_ms)
    out = g.agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"))
    out.index.name = "open_time"
    return out.reset_index()


def _context_at(cfg: Config, df, ts: int, symbol: str):
    st = SymbolState(symbol, cfg.strategy, cfg.scanner)
    for r in df[df.open_time < ts].itertuples(index=False):
        st.on_bar(Bar(symbol, int(r.open_time), r.open, r.high, r.low, r.close, r.volume, r.quote_volume))
    return st.level_context()


def _tf_label(minutes: int) -> str:
    return f"{minutes // 60}h" if minutes % 60 == 0 else f"{minutes}m"


def _draw_trade(cfg: Config, store: HistoricalStore, t: Trade, ax, ax_rsi) -> None:
    from autotr.data.replay import warmup_days

    sc = cfg.strategy
    tf = sc.entry_tf_minutes * MINUTE_MS
    sig_ts = t.signal_ts or t.entry_ts
    hist = _bars_df(store, t.symbol, sig_ts - warmup_days(cfg) * DAY_MS, t.exit_ts + 12 * tf)
    ctx = _context_at(cfg, hist, sig_ts, t.symbol)
    candles = _resample(hist, tf)
    rs_all = rsi_series(candles.close.to_numpy(), sc.rsi_period)
    win_mask = (candles.open_time >= sig_ts - 60 * tf) & (candles.open_time < t.exit_ts + 12 * tf)
    w = candles[win_mask]
    rs = rs_all[win_mask.to_numpy()]
    x = np.array([_dt(int(v)) for v in w.open_time])
    t_first, t_last = int(w.open_time.iloc[0]), int(w.open_time.iloc[-1])

    lo, hi = w.low.min(), w.high.max()
    pad = (hi - lo) * 0.15
    if ctx is not None:
        for z in ctx.zones:
            if z.high >= lo - pad and z.low <= hi + pad:
                ax.axhspan(z.low, z.high, color=ZONE, alpha=0.55, linewidth=0, zorder=0)
        for line in (ctx.support_line, ctx.resistance_line):
            if line is None:
                continue
            ts0 = max(line.t1, t_first)
            ax.plot([_dt(ts0), _dt(t_last)], [line.value_at(ts0), line.value_at(t_last)], color=VIOLET,
                    linewidth=2, solid_capstyle="round", zorder=2)
        ch = ctx.channel
        if ch is not None:
            ts0 = max(ch.t0, t_first)
            xs = [_dt(ts0), _dt(t_last)]
            ax.fill_between(xs, [ch.lower(ts0), ch.lower(t_last)], [ch.upper(ts0), ch.upper(t_last)],
                            color=AQUA, alpha=0.08, linewidth=0, zorder=1)
            for f in (ch.lower, ch.upper):
                ax.plot(xs, [f(ts0), f(t_last)], color=AQUA, linewidth=1.6, zorder=2)
        imp = ctx.impulse
        if imp is not None:
            pts = [p for p in imp.points if p.ts >= t_first]
            if pts:
                ax.plot([_dt(p.ts) for p in pts], [p.price for p in pts], color=ORANGE, linewidth=1.2,
                        zorder=2, marker="o", markersize=4)
                for i, p in enumerate(imp.points):
                    if p.ts >= t_first:
                        ax.annotate(str(i), (_dt(p.ts), p.price), xytext=(0, 6 if p.kind == "high" else -12),
                                    textcoords="offset points", ha="center", fontsize=8, color=TEXT)

    width = 0.6 * sc.entry_tf_minutes / (24 * 60)
    up = (w.close >= w.open).to_numpy()
    ax.vlines(x, w.low, w.high, color=CANDLE_DOWN, linewidth=0.7, zorder=3)
    ax.bar(x[up], (w.close - w.open)[up], bottom=w.open[up], width=width, color=CANDLE_UP,
           edgecolor=CANDLE_DOWN, linewidth=0.6, zorder=4)
    ax.bar(x[~up], (w.close - w.open)[~up], bottom=w.open[~up], width=width, color=CANDLE_DOWN,
           linewidth=0, zorder=4)

    t0, t1 = _dt(t.entry_ts), _dt(t.exit_ts)
    t_lvl = _dt(max(t.exit_ts, t.entry_ts + 3 * tf))
    ax.hlines(t.stop, _dt(sig_ts), t_lvl, color=RED, linewidth=1.6, linestyles=(0, (4, 2)), zorder=5)
    ax.hlines(t.take_profit, _dt(sig_ts), t_lvl, color=BLUE, linewidth=1.6, linestyles=(0, (4, 2)), zorder=5)
    # ladder rungs at signal time: filled (nearest first) solid, unfilled hollow
    for i, price in enumerate(t.rungs):
        filled = i < t.fills
        ax.plot([_dt(sig_ts)], [price], marker="o", markersize=7, linestyle="none", zorder=6,
                color=TEXT if filled else SURFACE, markeredgecolor=TEXT, markeredgewidth=1.4)

    win = t.net_pnl > 0
    ax.plot([t0], [t.entry_price], marker="^" if t.side is Side.LONG else "v", markersize=10,
            color=TEXT, markeredgecolor=SURFACE, markeredgewidth=2, zorder=7, linestyle="none")
    ax.plot([t1], [t.exit_price], marker="o", markersize=9, color=BLUE if win else RED,
            markeredgecolor=SURFACE, markeredgewidth=2, zorder=7, linestyle="none")
    ax.set_xlim(x[0], x[-1])
    ax.set_ylim(min(lo, t.stop, t.take_profit) - pad * 0.5, max(hi, t.stop, t.take_profit) + pad * 0.5)
    hold = (t.exit_ts - t.entry_ts) / MINUTE_MS
    hold_s = f"{hold / 60:.1f} h" if hold >= 120 else f"{hold:.0f} min"
    fills = f", {t.fills}/{len(t.rungs)} rungs" if t.rungs else ""
    ax.set_title(f"{t.side.value} {t.symbol.replace('USDT', '')} · {t.exit_reason.replace('_', ' ')} after "
                 f"{hold_s}{fills} · ${t.net_pnl:+.2f} ({t.r_multiple:+.2f}R)", fontsize=9)
    import textwrap
    why = t.entry_reason.split(" | R:R")[0].split(" (R:R")[0]
    ax.text(0.01, 0.02, textwrap.fill(why, 70), transform=ax.transAxes, fontsize=7, color=TEXT_2, va="bottom",
            bbox=dict(facecolor=SURFACE, edgecolor="none", alpha=0.8, pad=1.5), zorder=8)
    ax.tick_params(labelbottom=False)

    ax_rsi.plot(x, rs, color=TEXT_2, linewidth=1.3)
    ax_rsi.axhspan(sc.rsi_overbought, 100, color=RED, alpha=0.07, linewidth=0)
    ax_rsi.axhspan(0, sc.rsi_oversold, color=BLUE, alpha=0.07, linewidth=0)
    for lvl in (sc.rsi_oversold, sc.rsi_overbought):
        ax_rsi.axhline(lvl, color=MUTED, linewidth=0.8)
    ax_rsi.axvline(_dt(sig_ts), color=TEXT, linewidth=0.8, linestyle=(0, (2, 2)))
    ax_rsi.set_ylim(0, 100)
    ax_rsi.set_yticks([sc.rsi_oversold, sc.rsi_overbought])
    ax_rsi.set_ylabel(f"RSI {_tf_label(sc.entry_tf_minutes)}", fontsize=8)
    fmt = "%m-%d %H:%M" if (t_last - t_first) > DAY_MS else "%H:%M"
    ax_rsi.xaxis.set_major_formatter(mdates.DateFormatter(fmt, tz=timezone.utc))
    ax_rsi.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=5))
    ax_rsi.set_xlim(x[0], x[-1])


def plot_trade_gallery(cfg: Config, trades: list[Trade], path: str | Path, title: str) -> int:
    """Draw the 3 best take-profit trades and 3 typical (~-1R) stop-outs on entry-TF candles."""
    chosen = _pick_trades(trades)
    if not chosen:
        return 0
    store = HistoricalStore(cfg.data.dir)
    cols = 3
    rows = (len(chosen) + cols - 1) // cols
    fig = plt.figure(figsize=(18, 5.6 * rows), constrained_layout=True)
    outer = fig.add_gridspec(rows, cols)
    for i, t in enumerate(chosen):
        inner = outer[i // cols, i % cols].subgridspec(2, 1, height_ratios=[3, 1], hspace=0.05)
        ax = fig.add_subplot(inner[0])
        ax_rsi = fig.add_subplot(inner[1], sharex=ax)
        _draw_trade(cfg, store, t, ax, ax_rsi)
    st = _tf_label(cfg.strategy.structure_tf_minutes)
    legend = [
        Line2D([], [], marker="^", color=TEXT, linestyle="none", markersize=8, label="first fill (▲ long / ▼ short)"),
        Line2D([], [], marker="o", color=TEXT, linestyle="none", markersize=7, label="ladder rung filled"),
        Line2D([], [], marker="o", color=SURFACE, markeredgecolor=TEXT, linestyle="none", markersize=7,
               label="ladder rung not filled"),
        Line2D([], [], marker="o", color=BLUE, linestyle="none", markersize=8, label="exit in profit"),
        Line2D([], [], marker="o", color=RED, linestyle="none", markersize=8, label="exit at a loss"),
        Line2D([], [], color=BLUE, linestyle=(0, (4, 2)), linewidth=1.6, label="take profit"),
        Line2D([], [], color=RED, linestyle=(0, (4, 2)), linewidth=1.6, label="stop loss"),
        Line2D([], [], color=VIOLET, linewidth=2, label=f"trendline ({st})"),
        Line2D([], [], color=AQUA, linewidth=1.6, label=f"channel ({st})"),
        Line2D([], [], color=ORANGE, linewidth=1.2, marker="o", markersize=4, label=f"Elliott 0-5 ({st})"),
        Patch(color=ZONE, label=f"S/R zone ({st})"),
    ]
    fig.suptitle(title, x=0.01, ha="left", fontsize=13, fontweight="bold")
    fig.legend(handles=legend, loc="outside lower center", ncol=6, fontsize=9)
    fig.savefig(path, dpi=105)
    plt.close(fig)
    return len(chosen)
