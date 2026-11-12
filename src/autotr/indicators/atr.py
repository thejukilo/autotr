"""Average True Range (Wilder smoothing), incremental."""

from __future__ import annotations

from collections import deque


def true_range(high: float, low: float, prev_close: float | None) -> float:
    if prev_close is None:
        return high - low
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


class ATR:
    def __init__(self, period: int = 14):
        self.period = period
        self._prev_close: float | None = None
        self._seed: list[float] = []
        self.value: float | None = None

    def update(self, high: float, low: float, close: float) -> float | None:
        tr = true_range(high, low, self._prev_close)
        self._prev_close = close
        if self.value is None:
            self._seed.append(tr)
            if len(self._seed) == self.period:
                self.value = sum(self._seed) / self.period
                self._seed.clear()
        else:
            self.value = (self.value * (self.period - 1) + tr) / self.period
        return self.value


class RollingATRPct:
    """Simple mean of true range over the last N bars, as a fraction of the last close.

    Used by the volatility scanner ("how much does this coin move per 5m bar, in %").
    """

    def __init__(self, period: int):
        self.period = period
        self._prev_close: float | None = None
        self._trs: deque[float] = deque(maxlen=period)
        self._sum = 0.0
        self._last_close = 0.0

    def update(self, high: float, low: float, close: float) -> None:
        tr = true_range(high, low, self._prev_close)
        self._prev_close = close
        self._last_close = close
        if len(self._trs) == self.period:
            self._sum -= self._trs[0]
        self._trs.append(tr)
        self._sum += tr

    @property
    def ready(self) -> bool:
        return len(self._trs) == self.period

    @property
    def value(self) -> float | None:
        if not self.ready or self._last_close <= 0:
            return None
        return self._sum / self.period / self._last_close
