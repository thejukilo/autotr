"""Trendline detection through pivot points.

A *support* trendline joins two ascending pivot lows; a *resistance* trendline joins two
descending pivot highs. A line is only valid if no bar *closed* through it (beyond a small
tolerance) from its first anchor up to now. Among valid lines we prefer the one with the most
touches, then the most recent second anchor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

import numpy as np

from autotr.indicators.pivots import Pivot

LineKind = Literal["support", "resistance"]


@dataclass(frozen=True, slots=True)
class Trendline:
    t1: int
    p1: float
    t2: int
    p2: float
    kind: LineKind
    touches: int = 2

    @property
    def slope(self) -> float:
        return (self.p2 - self.p1) / (self.t2 - self.t1)

    def value_at(self, ts: int) -> float:
        return self.p1 + self.slope * (ts - self.t1)


def _valid(line: Trendline, times: np.ndarray, closes: np.ndarray, tol: float) -> bool:
    start = int(np.searchsorted(times, line.t1))
    t, c = times[start:], closes[start:]
    if len(t) == 0:
        return True
    v = line.p1 + line.slope * (t - line.t1)
    if line.kind == "support":
        return not bool(np.any(c < v - tol))
    return not bool(np.any(c > v + tol))


def _count_touches(line: Trendline, pivots: Sequence[Pivot], tol: float) -> int:
    return sum(1 for p in pivots if p.ts >= line.t1 and abs(p.price - line.value_at(p.ts)) <= tol)


def find_trendline(pivots: Sequence[Pivot], kind: LineKind, times: Sequence[int],
                   closes: Sequence[float], lookback: int, break_tol: float,
                   touch_tol: float) -> Trendline | None:
    """Best valid trendline among the last `lookback` pivots of the matching kind.

    `times`/`closes` are the bar series (same timeframe as the pivots) used for validation.
    """
    times_a = np.asarray(times, dtype=np.float64)
    closes_a = np.asarray(closes, dtype=np.float64)
    pk = "low" if kind == "support" else "high"
    pts = [p for p in pivots if p.kind == pk][-lookback:]
    best: Trendline | None = None
    best_key: tuple[int, int] | None = None
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            a, b = pts[i], pts[j]
            if kind == "support" and not b.price > a.price:
                continue
            if kind == "resistance" and not b.price < a.price:
                continue
            line = Trendline(a.ts, a.price, b.ts, b.price, kind)
            if not _valid(line, times_a, closes_a, break_tol):
                continue
            touches = _count_touches(line, pts, touch_tol)
            key = (touches, b.ts)
            if best_key is None or key > best_key:
                best = Trendline(a.ts, a.price, b.ts, b.price, kind, touches)
                best_key = key
    return best
