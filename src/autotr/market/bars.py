"""Incremental aggregation of 1m bars into higher timeframes."""

from __future__ import annotations

from autotr.models import Bar


class BarAggregator:
    """Builds N-minute bars from a stream of 1m bars aligned to UTC boundaries.

    `update()` returns the completed higher-timeframe bar when the incoming 1m bar is the last one
    of its bucket, otherwise None. A gap in the 1m stream that skips a bucket's final minute still
    flushes the partial bar when the next bucket starts.
    """

    def __init__(self, interval_ms: int):
        self.interval_ms = interval_ms
        self._cur: Bar | None = None

    def update(self, bar: Bar) -> list[Bar]:
        done: list[Bar] = []
        bucket = bar.open_time - bar.open_time % self.interval_ms
        cur = self._cur
        if cur is not None and cur.open_time != bucket:
            done.append(cur)
            cur = None
        if cur is None:
            cur = Bar(bar.symbol, bucket, bar.open, bar.high, bar.low, bar.close,
                      bar.volume, bar.quote_volume, self.interval_ms)
        else:
            cur.high = max(cur.high, bar.high)
            cur.low = min(cur.low, bar.low)
            cur.close = bar.close
            cur.volume += bar.volume
            cur.quote_volume += bar.quote_volume
        if bar.close_time >= bucket + self.interval_ms:
            done.append(cur)
            cur = None
        self._cur = cur
        return done
