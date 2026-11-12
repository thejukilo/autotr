"""Market events produced by feeds (Binance WebSocket or historical replay)."""

from __future__ import annotations

from dataclasses import dataclass, field

from autotr.models import Bar


@dataclass(slots=True)
class WarmupBars:
    """History for (new) symbols: feeds indicators only, never trades. Bars may be any interval
    (1h/5m klines from REST, then 1m); each bar carries its own `interval_ms`."""
    bars: list[Bar]


@dataclass(slots=True)
class KlineClosed:
    bar: Bar
    funding_rate: float | None = None  # replay only: funding event at this bar's open time
    backfill: bool = False             # recovered after a reconnect: update features, don't trade


@dataclass(slots=True)
class Quote:
    symbol: str
    bid: float
    ask: float
    ts: int


@dataclass(slots=True)
class Funding:
    symbol: str
    rate: float
    mark_price: float
    ts: int


@dataclass(slots=True)
class Volumes24h:
    """24h ticker snapshot: quote volume and fractional price change per symbol."""
    quote_volume: dict[str, float] = field(default_factory=dict)
    change: dict[str, float] = field(default_factory=dict)


Event = WarmupBars | KlineClosed | Quote | Funding | Volumes24h
