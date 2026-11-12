"""Core value types shared by every module."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

MINUTE_MS = 60_000
FIVE_MIN_MS = 5 * MINUTE_MS
HOUR_MS = 60 * MINUTE_MS
DAY_MS = 24 * HOUR_MS


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def sign(self) -> int:
        return 1 if self is Side.LONG else -1


@dataclass(slots=True)
class Bar:
    symbol: str
    open_time: int  # ms, UTC
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    interval_ms: int = MINUTE_MS

    @property
    def close_time(self) -> int:
        return self.open_time + self.interval_ms


@dataclass(slots=True)
class Signal:
    symbol: str
    side: Side
    ts: int          # ms, time the signal was generated (bar close)
    ref_price: float  # close of the signal bar
    stop: float
    take_profit: float
    reason: str
    # Optional limit-order ladder: [(price, weight), ...] nearest rung first; weights sum to 1.
    ladder: list[tuple[float, float]] | None = None


@dataclass(slots=True)
class Trade:
    symbol: str
    side: Side
    qty: float
    entry_price: float
    exit_price: float
    entry_ts: int
    exit_ts: int
    gross_pnl: float
    fees: float
    funding: float
    exit_reason: str
    risk_usd: float
    entry_reason: str = ""
    stop: float = 0.0
    take_profit: float = 0.0
    fills: int = 1           # ladder rungs filled
    signal_ts: int = 0       # when the strategy signalled (entry_ts is the first fill)
    rungs: tuple[float, ...] = ()  # planned limit prices, nearest first (empty = market entry)

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.fees + self.funding

    @property
    def r_multiple(self) -> float:
        return self.net_pnl / self.risk_usd if self.risk_usd > 0 else 0.0


@dataclass(slots=True)
class EquityPoint:
    ts: int
    equity: float
    balance: float
    open_positions: int = 0
    extra: dict = field(default_factory=dict)
