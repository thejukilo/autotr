"""Per-symbol streaming feature state: indicators on 1m and 5m, pivots, levels, trendlines.

Everything is updated incrementally bar by bar so the exact same object serves the backtest and
the live engine. Expensive structures (zones, trendlines) are computed lazily and cached per
completed 5m bar, so they only cost CPU when the strategy actually asks for them.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from autotr.config import ScannerCfg, StrategyCfg
from autotr.indicators.atr import ATR, RollingATRPct
from autotr.indicators.levels import Zone, build_zones
from autotr.indicators.pivots import Pivot, latest_pivots
from autotr.indicators.rsi import RSI
from autotr.indicators.trendlines import Trendline, find_trendline
from autotr.market.bars import BarAggregator
from autotr.models import DAY_MS, FIVE_MIN_MS, MINUTE_MS, Bar


@dataclass(slots=True)
class LevelContext:
    zones: list[Zone]
    support_line: Trendline | None
    resistance_line: Trendline | None
    atr5: float
    last5_close: float
    last5_open_time: int


class SymbolState:
    def __init__(self, symbol: str, cfg: StrategyCfg, scan_cfg: ScannerCfg):
        self.symbol = symbol
        self.cfg = cfg
        self.rsi = RSI(cfg.rsi_period)
        self.atr1 = ATR(cfg.atr_period)
        self.atr5 = ATR(cfg.atr_period)
        self.ema5: float | None = None
        self._ema_hist: deque[float] = deque(maxlen=cfg.trend_slope_bars + 1)
        self._ema_n = 0
        self.agg5 = BarAggregator(FIVE_MIN_MS)
        self.scan_atr = RollingATRPct(scan_cfg.atr_period_bars)

        self.bars1: deque[Bar] = deque(maxlen=max(cfg.touch_lookback_bars, 5))
        n5 = cfg.history_5m_bars
        self.t5: deque[int] = deque(maxlen=n5)
        self.h5: deque[float] = deque(maxlen=n5)
        self.l5: deque[float] = deque(maxlen=n5)
        self.c5: deque[float] = deque(maxlen=n5)
        self.pivots: deque[Pivot] = deque()

        self.rsi_prev: float | None = None
        self.rsi_now: float | None = None

        self._qv: deque[tuple[int, float]] = deque()
        self._qv_sum = 0.0

        self.last_bar: Bar | None = None
        self._ctx: LevelContext | None = None
        self._ctx_key: int | None = None

    # ------------------------------------------------------------------ updates
    def on_bar(self, bar: Bar) -> list[Bar]:
        """Feed a closed 1m bar. Returns any 5m bars completed by it."""
        self.last_bar = bar
        self.bars1.append(bar)
        self.rsi_prev = self.rsi_now
        self.rsi_now = self.rsi.update(bar.close)
        self.atr1.update(bar.high, bar.low, bar.close)

        self._qv.append((bar.open_time, bar.quote_volume))
        self._qv_sum += bar.quote_volume
        cutoff = bar.open_time - DAY_MS
        while self._qv and self._qv[0][0] <= cutoff:
            self._qv_sum -= self._qv.popleft()[1]

        done = self.agg5.update(bar)
        for b5 in done:
            self._on_bar5(b5)
        return done

    def _on_bar5(self, b: Bar) -> None:
        self.atr5.update(b.high, b.low, b.close)
        k = 2 / (self.cfg.trend_ema_period + 1)
        self.ema5 = b.close if self.ema5 is None else self.ema5 + k * (b.close - self.ema5)
        self._ema_n += 1
        self._ema_hist.append(self.ema5)
        self.scan_atr.update(b.high, b.low, b.close)
        self.t5.append(b.open_time)
        self.h5.append(b.high)
        self.l5.append(b.low)
        self.c5.append(b.close)
        c = self.cfg
        window = c.pivot_left + c.pivot_right + 1
        if len(self.h5) >= window:
            # only the tail is needed to confirm the newest pivot
            tail_t = list(self.t5)[-window:]
            tail_h = list(self.h5)[-window:]
            tail_l = list(self.l5)[-window:]
            self.pivots.extend(latest_pivots(tail_t, tail_h, tail_l, c.pivot_left, c.pivot_right))
        oldest = self.t5[0]
        while self.pivots and self.pivots[0].ts < oldest:
            self.pivots.popleft()

    # ------------------------------------------------------------------ queries
    @property
    def ema_slope(self) -> float | None:
        """Change of the 5m EMA over `trend_slope_bars` bars (None until the EMA has settled)."""
        if self._ema_n < self.cfg.trend_ema_period or len(self._ema_hist) < self._ema_hist.maxlen:
            return None
        return self._ema_hist[-1] - self._ema_hist[0]

    @property
    def ready(self) -> bool:
        return (
            self.rsi_now is not None
            and self.atr1.value is not None
            and self.atr5.value is not None
            and len(self.c5) >= self.cfg.pivot_left + self.cfg.pivot_right + 10
        )

    @property
    def quote_volume_24h(self) -> float:
        return self._qv_sum

    @property
    def has_24h_history(self) -> bool:
        return bool(self._qv) and self._qv[-1][0] - self._qv[0][0] >= DAY_MS - 2 * MINUTE_MS

    @property
    def volatility(self) -> float | None:
        """Mean 5m true range over the scanner window, as a fraction of price."""
        return self.scan_atr.value

    def level_context(self) -> LevelContext | None:
        if not self.ready or not self.t5:
            return None
        key = self.t5[-1]
        if self._ctx_key == key and self._ctx is not None:
            return self._ctx
        c = self.cfg
        atr5 = float(self.atr5.value)  # type: ignore[arg-type]
        pivots = list(self.pivots)
        zones = build_zones(pivots, c.zone_tolerance_atr * atr5, c.zone_min_touches)
        times, closes = list(self.t5), list(self.c5)
        kw = dict(lookback=c.trendline_lookback_pivots,
                  break_tol=c.trendline_break_tolerance_atr * atr5,
                  touch_tol=c.zone_tolerance_atr * atr5)
        sup = find_trendline(pivots, "support", times, closes, **kw)
        res = find_trendline(pivots, "resistance", times, closes, **kw)
        self._ctx = LevelContext(zones, sup, res, atr5, closes[-1], times[-1])
        self._ctx_key = key
        return self._ctx
