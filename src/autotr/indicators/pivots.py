"""Swing-high / swing-low (fractal) pivot detection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

PivotKind = Literal["high", "low"]


@dataclass(frozen=True, slots=True)
class Pivot:
    ts: int        # open time of the pivot bar (ms)
    price: float
    kind: PivotKind


def is_pivot_high(highs: Sequence[float], i: int, left: int, right: int) -> bool:
    h = highs[i]
    # strictly higher than the left side, >= the right side (ties resolve to the earlier bar)
    return all(h > highs[j] for j in range(i - left, i)) and all(h >= highs[j] for j in range(i + 1, i + right + 1))


def is_pivot_low(lows: Sequence[float], i: int, left: int, right: int) -> bool:
    lo = lows[i]
    return all(lo < lows[j] for j in range(i - left, i)) and all(lo <= lows[j] for j in range(i + 1, i + right + 1))


def find_pivots(times: Sequence[int], highs: Sequence[float], lows: Sequence[float],
                left: int, right: int) -> list[Pivot]:
    """All confirmed pivots in a series (a pivot needs `right` bars after it to be confirmed)."""
    out: list[Pivot] = []
    for i in range(left, len(highs) - right):
        if is_pivot_high(highs, i, left, right):
            out.append(Pivot(times[i], highs[i], "high"))
        if is_pivot_low(lows, i, left, right):
            out.append(Pivot(times[i], lows[i], "low"))
    return out


def latest_pivots(times: Sequence[int], highs: Sequence[float], lows: Sequence[float],
                  left: int, right: int) -> list[Pivot]:
    """Pivots confirmed by the most recent bar (checks only index len-1-right). For streaming."""
    i = len(highs) - 1 - right
    if i < left:
        return []
    out: list[Pivot] = []
    if is_pivot_high(highs, i, left, right):
        out.append(Pivot(times[i], highs[i], "high"))
    if is_pivot_low(lows, i, left, right):
        out.append(Pivot(times[i], lows[i], "low"))
    return out
