"""Opening-range breakout on movers at fixed UTC session opens (5m), market entry, trailing exit.

For each session start S in `sessions` (minutes after 00:00 UTC; default 00:00 and 13:30):
OR = the `or_bars` 5m bars from S. Valid if OR height is in [min_h, max_h] of price and OR volume
>= vol_mult x the mean volume of an equal-length block over the bars before S (~ prior day).
Mover filter |change_24h| >= min_change, side = sign(change_24h), close on that side of VWAP24.
Trigger: a 5m close beyond the OR high (long) / low (short) within `window_min` after the OR ends.
One trade per symbol per session. Stop: opposite OR side (OR midpoint if height > mid_if), widened
to at least `min_stop`. No target. manage(): chandelier trail_atr x ATR5 from the best close once
the trade is +trail_after_r R.
Hypothesis: ORB on "stocks in play" (Zarattini et al. 2024) transfers to crypto movers at the
daily-candle open and the US open.
"""

from __future__ import annotations

from autotr.models import DAY_MS, MINUTE_MS, Side
from autotr.strategy.research._common import signal
from autotr.strategy.research._scalp import make_book, atr_bars, best_close, far_target, vwap

DEFAULTS = dict(sessions=[0, 810], or_bars=6, min_h=0.01, max_h=0.05, vol_mult=2.0, min_change=0.10,
                window_min=120, mid_if=0.04, min_stop=0.015, trail_atr=3.0, trail_after_r=1.0, atr_n=14,
                min_bars=200)


class Strategy:
    def __init__(self, cfg, fees):
        self.cfg = cfg
        self.p = {**DEFAULTS, **cfg.params}
        self.book = make_book(cfg)
        self.done: set[tuple[str, int]] = set()

    def on_bar(self, st):
        p = self.p
        bars = list(st.bars_e)
        ts = st.last_bar.close_time
        ch = st.change_24h
        if ch is None or abs(ch) < p["min_change"] or len(bars) < p["min_bars"] or not self.book.can_trade(st.symbol, ts):
            return None
        day0 = ts - ts % DAY_MS
        tf = st.entry_ms
        for m in p["sessions"]:
            for start in (day0 + m * MINUTE_MS, day0 - DAY_MS + m * MINUTE_MS):
                or_end = start + p["or_bars"] * tf
                if or_end < ts <= or_end + p["window_min"] * MINUTE_MS:
                    sig = self._check(st, bars, ts, ch, start, or_end)
                    if sig is not None:
                        return sig
        return None

    def _check(self, st, bars, ts, ch, start, or_end):
        p = self.p
        key = (st.symbol, start)
        if key in self.done:
            return None
        orb = [b for b in bars if start <= b.open_time < or_end]
        prior = [b for b in bars if b.open_time < start]
        if len(orb) < p["or_bars"] or len(prior) < 4 * p["or_bars"]:
            return None
        hi, lo = max(b.high for b in orb), min(b.low for b in orb)
        c = bars[-1].close
        h = (hi - lo) / c
        if not p["min_h"] <= h <= p["max_h"]:
            return None
        base = sum(b.volume for b in prior) / len(prior) * len(orb)
        if sum(b.volume for b in orb) < p["vol_mult"] * base:
            return None
        vw = vwap(bars)
        if vw is None:
            return None
        if ch > 0 and c > hi and c > vw:
            side, stop = Side.LONG, ((hi + lo) / 2 if h > p["mid_if"] else lo)
            stop = min(stop, c * (1 - p["min_stop"]))
        elif ch < 0 and c < lo and c < vw:
            side, stop = Side.SHORT, ((hi + lo) / 2 if h > p["mid_if"] else hi)
            stop = max(stop, c * (1 + p["min_stop"]))
        else:
            return None
        hour = (start % DAY_MS) // MINUTE_MS
        sig = signal(st.symbol, side, ts, c, stop, far_target(side.sign, c, stop),
                     f"ORB {hour // 60:02d}:{hour % 60:02d} h={h:.1%} ch={ch:+.0%}")
        if sig is not None:
            self.done.add(key)
        return sig

    def manage(self, st, pos):
        p = self.p
        risk = self.book.risk(st, pos)
        best = best_close(st, pos)
        a = atr_bars(list(st.bars_e), p["atr_n"])
        s = pos.side.sign
        if best is None or a <= 0 or risk <= 0 or s * (best - pos.entry_price) < p["trail_after_r"] * risk:
            return None
        return best - s * p["trail_atr"] * a
