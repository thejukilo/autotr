"""Price channels: two parallel lines that price has respected on both sides.

Construction: every pair of same-kind pivots defines a candidate base line (through two lows → the
channel floor, through two highs → the ceiling). The opposite boundary is the parallel line through
the most extreme opposite pivot since the first anchor. The channel is valid when
  * each boundary has at least `min_touches` pivots within `touch_tol`, and
  * no bar closed outside either boundary (beyond `break_tol`) since the first anchor.
Among valid channels we prefer more touches, then the most recent anchor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

from autotr.indicators.pivots import Pivot


@dataclass(frozen=True, slots=True)
class Channel:
    t0: int            # first anchor time
    base: float        # lower boundary value at t0
    width: float       # vertical distance upper - lower (constant)
    slope: float       # price per ms
    touches_low: int
    touches_high: int

    def lower(self, ts: int) -> float:
        return self.base + self.slope * (ts - self.t0)

    def upper(self, ts: int) -> float:
        return self.lower(ts) + self.width

    def mid(self, ts: int) -> float:
        return self.lower(ts) + self.width / 2

    @property
    def touches(self) -> int:
        return self.touches_low + self.touches_high


def _touches(pivots: Sequence[Pivot], kind: str, t0: int, value_at, tol: float) -> int:
    return sum(1 for p in pivots if p.kind == kind and p.ts >= t0 and abs(p.price - value_at(p.ts)) <= tol)


def find_channel(pivots: Sequence[Pivot], times: Sequence[int], closes: Sequence[float],
                 lookback: int, touch_tol: float, break_tol: float, min_touches: int = 2,
                 min_width: float = 0.0) -> Channel | None:
    pts = list(pivots)[-lookback:]
    if len(pts) < 4:
        return None
    t_arr = np.asarray(times, dtype=np.float64)
    c_arr = np.asarray(closes, dtype=np.float64)
    best: Channel | None = None
    best_key: tuple | None = None
    for kind, other in (("low", "high"), ("high", "low")):
        same = [p for p in pts if p.kind == kind]
        opp = [p for p in pts if p.kind == other]
        for i in range(len(same)):
            for j in range(i + 1, len(same)):
                a, b = same[i], same[j]
                if b.ts == a.ts:
                    continue
                slope = (b.price - a.price) / (b.ts - a.ts)
                base_line = lambda ts, a=a, s=slope: a.price + s * (ts - a.ts)  # noqa: E731
                cands = [p for p in opp if p.ts >= a.ts]
                if not cands:
                    continue
                # extreme opposite pivot relative to the base line
                offsets = [p.price - base_line(p.ts) for p in cands]
                off = max(offsets) if kind == "low" else min(offsets)
                width = abs(off)
                if width <= max(min_width, 2 * touch_tol):
                    continue
                if kind == "low":
                    ch = Channel(a.ts, a.price, width, slope, 0, 0)
                else:
                    ch = Channel(a.ts, a.price - width, width, slope, 0, 0)
                tl = _touches(pts, "low", a.ts, ch.lower, touch_tol)
                th = _touches(pts, "high", a.ts, ch.upper, touch_tol)
                if tl < min_touches or th < min_touches:
                    continue
                start = int(np.searchsorted(t_arr, a.ts))
                tt, cc = t_arr[start:], c_arr[start:]
                lo = ch.base + slope * (tt - ch.t0)
                if np.any(cc < lo - break_tol) or np.any(cc > lo + width + break_tol):
                    continue
                key = (tl + th, b.ts)
                if best_key is None or key > best_key:
                    best = Channel(ch.t0, ch.base, width, slope, tl, th)
                    best_key = key
    return best
