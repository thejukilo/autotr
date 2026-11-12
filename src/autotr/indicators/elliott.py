"""Rule-based Elliott-wave impulse detection (used to fade a completed wave 5).

Elliott counting is subjective; this is a strict, mechanical approximation:

1. Turn pivots into a zigzag (alternating high/low; when two of the same kind are adjacent keep
   the more extreme one).
2. Test the last six zigzag points p0..p5 as a five-wave impulse.

Hard rules (bullish impulse; bearish is mirrored):
  * wave 2 does not retrace beyond the start of wave 1            (p2 > p0)
  * wave 3 goes beyond the end of wave 1                         (p3 > p1)
  * wave 3 is not the shortest of waves 1, 3, 5
  * wave 4 does not enter wave-1 territory                        (p4 > p1)  [strict mode]
  * wave 5 goes beyond the end of wave 3                         (p5 > p3)
Guidelines add to `quality` (0..1): wave 2 retraces 38.2–78.6% of wave 1, wave 4 retraces
23.6–50% of wave 3, wave 3 is the longest.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

from autotr.indicators.pivots import Pivot


@dataclass(frozen=True, slots=True)
class Impulse:
    direction: Literal["up", "down"]
    points: tuple[Pivot, ...]   # p0..p5
    quality: float              # 0..1, share of guidelines met

    @property
    def end(self) -> Pivot:
        return self.points[5]

    @property
    def wave4(self) -> Pivot:
        return self.points[4]

    @property
    def fade_side(self) -> Literal["LONG", "SHORT"]:
        """A completed up-impulse is faded with a short, a down-impulse with a long."""
        return "SHORT" if self.direction == "up" else "LONG"


def zigzag(pivots: Sequence[Pivot]) -> list[Pivot]:
    out: list[Pivot] = []
    for p in sorted(pivots, key=lambda p: (p.ts, p.kind)):
        if out and out[-1].kind == p.kind:
            prev = out[-1]
            if (p.kind == "high" and p.price >= prev.price) or (p.kind == "low" and p.price <= prev.price):
                out[-1] = p
            continue
        if out and out[-1].ts == p.ts:
            # a bar that is both a pivot high and low: keep the one that continues the zigzag
            continue
        out.append(p)
    return out


def _check(pts: Sequence[Pivot], strict_overlap: bool) -> Impulse | None:
    p = [x.price for x in pts]
    up = pts[0].kind == "low"
    s = 1 if up else -1  # flip prices so we can always test a bullish impulse
    q = [s * v for v in p]
    w1, w2, w3, w4, w5 = q[1] - q[0], q[1] - q[2], q[3] - q[2], q[3] - q[4], q[5] - q[4]
    if min(w1, w2, w3, w4, w5) <= 0:
        return None
    if not q[2] > q[0]:
        return None                      # wave 2 retraced past the start
    if not q[3] > q[1]:
        return None                      # wave 3 must exceed wave 1's end
    if w3 < w1 and w3 < w5:
        return None                      # wave 3 is the shortest
    if strict_overlap and not q[4] > q[1]:
        return None                      # wave 4 overlaps wave 1
    if not q[5] > q[3]:
        return None                      # wave 5 must exceed wave 3's end (no truncation)
    guides = [
        0.382 <= w2 / w1 <= 0.786,
        0.236 <= w4 / w3 <= 0.5,
        w3 >= w1 and w3 >= w5,
    ]
    return Impulse("up" if up else "down", tuple(pts), sum(guides) / len(guides))


def find_impulse(pivots: Sequence[Pivot], strict_overlap: bool = True) -> Impulse | None:
    """Completed impulse ending at the most recent zigzag point, or None."""
    zz = zigzag(pivots)
    if len(zz) < 6:
        return None
    return _check(zz[-6:], strict_overlap)
