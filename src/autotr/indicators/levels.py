"""Support / resistance zones from clustered pivots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from autotr.indicators.pivots import Pivot


@dataclass(slots=True)
class Zone:
    low: float
    high: float
    touches: int
    last_ts: int

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2


def build_zones(pivots: Iterable[Pivot], tolerance: float, min_touches: int = 2) -> list[Zone]:
    """Cluster pivot prices (highs and lows together — old resistance becomes support).

    Pivots are sorted by price and greedily merged while within `tolerance` (absolute price) of the
    running cluster mean. Clusters with at least `min_touches` pivots become zones.
    Returned sorted by price ascending.
    """
    pts = sorted(pivots, key=lambda p: p.price)
    if not pts or tolerance <= 0:
        return []
    zones: list[Zone] = []
    cluster: list[Pivot] = [pts[0]]
    total = pts[0].price

    def flush() -> None:
        if len(cluster) >= min_touches:
            prices = [p.price for p in cluster]
            zones.append(Zone(min(prices), max(prices), len(cluster), max(p.ts for p in cluster)))

    for p in pts[1:]:
        if abs(p.price - total / len(cluster)) <= tolerance:
            cluster.append(p)
            total += p.price
        else:
            flush()
            cluster, total = [p], p.price
    flush()
    return zones


def nearest_below(zones: list[Zone], price: float) -> Zone | None:
    """Highest zone whose bottom is at or below `price` (price is above it or inside it)."""
    best = None
    for z in zones:
        if z.low <= price:
            best = z
        else:
            break
    return best


def nearest_above(zones: list[Zone], price: float) -> Zone | None:
    for z in zones:
        if z.high >= price:
            return z
    return None
