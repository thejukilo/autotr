"""Liquidation-flush catch (1m), trend-aligned, maker in / maker out.

A flush = the last `win` closed 1m bars cover a range D >= max(min_drop_pct, atr_mult x ATR1m(atr_n))
of price, with the high before the low (down-flush, for longs), on window volume >= vol_mult x the
normal volume (median 1m volume of the `vol_base` bars before the window), making a new
`new_extreme`-bar low, and the bar closing in the lower `close_frac` of the window range.
Longs only on up-movers (change_24h >= min_change) unless `both`; mirrored for shorts on down-movers.

Entry: limit rungs at close - r1*D and close - r2*D. Stop = min(win_low - stop_d*D, rung2*(1-stop_pct)),
skipped if more than `max_stop` from the average rung. Maker TP at win_low + tp_frac*D.
manage(): after `be_after_min`, move to break-even once the high since entry >= avg entry + be_d*D.
Hypothesis: forced liquidations overshoot; buying the wick in the day's direction with maker legs
has a gross edge large relative to its ~0.04% round-trip cost.
"""

from __future__ import annotations

from autotr.models import MINUTE_MS, Side
from autotr.strategy.research._common import signal
from autotr.strategy.research._scalp import make_book, atr_bars, best_extreme, median

DEFAULTS = dict(min_change=0.10, both=False, win=5, min_drop_pct=0.03, atr_mult=6.0, atr_n=60,
                vol_mult=4.0, vol_base=120, new_extreme=240, close_frac=0.4, r1=0.15, r2=0.35,
                stop_d=0.40, stop_pct=0.015, max_stop=0.06, tp_frac=0.5, be_after_min=30, be_d=0.25,
                max_per_day=2)


class Strategy:
    def __init__(self, cfg, fees):
        self.cfg = cfg
        self.p = {**DEFAULTS, **cfg.params}
        self.book = make_book(cfg, self.p["max_per_day"])

    def on_bar(self, st):
        p = self.p
        bars = list(st.bars_e)
        ts = st.last_bar.close_time
        ch = st.change_24h
        need = max(p["new_extreme"], p["vol_base"] + p["win"], p["atr_n"] + 1)
        if ch is None or abs(ch) < p["min_change"] or len(bars) < need or not self.book.can_trade(st.symbol, ts):
            return None
        w = bars[-p["win"]:]
        hi_i = max(range(len(w)), key=lambda i: w[i].high)
        lo_i = min(range(len(w)), key=lambda i: w[i].low)
        hi, lo = w[hi_i].high, w[lo_i].low
        d = hi - lo
        c = bars[-1].close
        if d <= 0 or c <= 0:
            return None
        a = atr_bars(bars[:-p["win"]], p["atr_n"])
        if d / c < max(p["min_drop_pct"], p["atr_mult"] * a / c):
            return None
        base = median([b.volume for b in bars[-p["win"] - p["vol_base"]:-p["win"]]])
        if base <= 0 or sum(b.volume for b in w) < p["vol_mult"] * p["win"] * base:
            return None
        ext = bars[-p["new_extreme"]:]
        frac = (c - lo) / d
        # long: down-flush in an up-mover
        if (ch > 0 or p["both"]) and hi_i < lo_i and lo <= min(b.low for b in ext) and frac <= p["close_frac"]:
            r1, r2 = c - p["r1"] * d, c - p["r2"] * d
            stop = min(lo - p["stop_d"] * d, r2 * (1 - p["stop_pct"]))
            avg = (r1 + r2) / 2
            if r2 <= 0 or stop <= 0 or (avg - stop) / avg > p["max_stop"]:
                return None
            sig = signal(st.symbol, Side.LONG, ts, c, stop, lo + p["tp_frac"] * d,
                         f"flush long D={d / c:.1%} ch={ch:+.0%}")
            if sig is not None:
                sig.ladder = [(r1, 0.5), (r2, 0.5)]
                self.book.record(st.symbol, ts, d=d)
            return sig
        # short: up-spike in a down-mover
        if (ch < 0 or p["both"]) and lo_i < hi_i and hi >= max(b.high for b in ext) and frac >= 1 - p["close_frac"]:
            r1, r2 = c + p["r1"] * d, c + p["r2"] * d
            stop = max(hi + p["stop_d"] * d, r2 * (1 + p["stop_pct"]))
            avg = (r1 + r2) / 2
            if (stop - avg) / avg > p["max_stop"]:
                return None
            sig = signal(st.symbol, Side.SHORT, ts, c, stop, hi - p["tp_frac"] * d,
                         f"flush short D={d / c:.1%} ch={ch:+.0%}")
            if sig is not None:
                sig.ladder = [(r1, 0.5), (r2, 0.5)]
                self.book.record(st.symbol, ts, d=d)
            return sig
        return None

    def manage(self, st, pos):
        self.book.touch(st, pos)
        p = self.p
        d = self.book.meta.get(st.symbol, {}).get("d")
        if d is None or st.last_bar.close_time - pos.entry_ts < p["be_after_min"] * MINUTE_MS:
            return None
        ext = best_extreme(st, pos)
        if ext is not None and pos.side.sign * (ext - pos.entry_price) >= p["be_d"] * d:
            return pos.entry_price
        return None
