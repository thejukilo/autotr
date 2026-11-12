"""Wilder's RSI — incremental (for streaming) and vectorised (for tests/analysis)."""

from __future__ import annotations

import numpy as np


class RSI:
    def __init__(self, period: int = 14):
        self.period = period
        self._prev: float | None = None
        self._gains = 0.0
        self._losses = 0.0
        self._n = 0
        self.avg_gain: float | None = None
        self.avg_loss: float | None = None
        self.value: float | None = None

    def update(self, close: float) -> float | None:
        if self._prev is None:
            self._prev = close
            return None
        change = close - self._prev
        self._prev = close
        gain, loss = max(change, 0.0), max(-change, 0.0)
        p = self.period
        if self.avg_gain is None:
            self._gains += gain
            self._losses += loss
            self._n += 1
            if self._n < p:
                return None
            self.avg_gain, self.avg_loss = self._gains / p, self._losses / p
        else:
            self.avg_gain = (self.avg_gain * (p - 1) + gain) / p
            self.avg_loss = (self.avg_loss * (p - 1) + loss) / p
        if self.avg_loss == 0:
            self.value = 100.0 if self.avg_gain > 0 else 50.0
        else:
            rs = self.avg_gain / self.avg_loss
            self.value = 100.0 - 100.0 / (1.0 + rs)
        return self.value


def rsi(closes: np.ndarray, period: int = 14) -> np.ndarray:
    """Vectorised RSI with the same seeding as the streaming version; NaN during warm-up."""
    out = np.full(len(closes), np.nan)
    ind = RSI(period)
    for i, c in enumerate(closes):
        v = ind.update(float(c))
        if v is not None:
            out[i] = v
    return out
