"""Per-symbol streaming feature state on two timeframes.

* **Entry timeframe** (`entry_tf_minutes`, e.g. 5m): RSI, ATR, recent bars — the strategy is
  evaluated when an entry bar closes.
* **Structure timeframe** (`structure_tf_minutes`, e.g. 1h): ATR, trend EMA, pivots and — lazily,
  once per structure bar — S/R zones, trendlines, channel and Elliott impulse.

The base input is always closed 1m bars (`on_bar`), so backtest, replay and live share one code
path. `warmup_bar()` additionally accepts pre-aggregated bars (live start-up fetches 1h/5m klines
from REST); a per-timeframe guard drops aggregated bars that were already ingested.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from autotr.config import ScannerCfg, StrategyCfg
from autotr.indicators.atr import ATR, RollingATRPct
from autotr.indicators.channels import Channel, find_channel
from autotr.indicators.elliott import Impulse, find_impulse
from autotr.indicators.levels import Zone, build_zones
from autotr.indicators.pivots import Pivot, latest_pivots
from autotr.indicators.rsi import RSI
from autotr.indicators.trendlines import Trendline, find_trendline
from autotr.market.bars import BarAggregator
from autotr.models import DAY_MS, FIVE_MIN_MS, MINUTE_MS, Bar


@dataclass(slots=True)
class LevelContext:
    """Structure-timeframe picture at the last completed structure bar."""
    zones: list[Zone]
    support_line: Trendline | None
    resistance_line: Trendline | None
    atr: float               # structure-timeframe ATR
    last_close: float        # last structure-bar close
    last_open_time: int      # its open time
    channel: Channel | None = None
    impulse: Impulse | None = None
    pivots: tuple[Pivot, ...] = ()


class SymbolState:
    def __init__(self, symbol: str, cfg: StrategyCfg, scan_cfg: ScannerCfg):
        self.symbol = symbol
        self.cfg = cfg
        self.entry_ms = cfg.entry_tf_minutes * MINUTE_MS
        self.struct_ms = cfg.structure_tf_minutes * MINUTE_MS
        self.agg_entry = BarAggregator(self.entry_ms)
        self.agg_struct = BarAggregator(self.struct_ms)
        self.agg_scan = BarAggregator(FIVE_MIN_MS)
        self._last_open: dict[int, int] = {}

        # entry timeframe
        self.rsi = RSI(cfg.rsi_period)
        self.rsi_prev: float | None = None
        self.rsi_now: float | None = None
        self.rsi_hist: deque[float] = deque(maxlen=max(cfg.rsi_lookback, 1) + 1)
        self.atr_e = ATR(cfg.atr_period)
        self.bars_e: deque[Bar] = deque(maxlen=max(cfg.touch_lookback_bars, cfg.rsi_lookback, cfg.entry_history_bars, 10))
        self.last_bar: Bar | None = None  # last closed entry-timeframe bar

        # structure timeframe
        self.atr_s = ATR(cfg.atr_period)
        self.ema: float | None = None
        self._ema_hist: deque[float] = deque(maxlen=cfg.trend_slope_bars + 1)
        self._ema_n = 0
        n = cfg.structure_history_bars
        self.t_s: deque[int] = deque(maxlen=n)
        self.h_s: deque[float] = deque(maxlen=n)
        self.l_s: deque[float] = deque(maxlen=n)
        self.c_s: deque[float] = deque(maxlen=n)
        self.pivots: deque[Pivot] = deque()

        # scanner inputs (always from 5m bars / 1m volume)
        self.scan_atr = RollingATRPct(scan_cfg.atr_period_bars)
        self._day: deque[tuple[int, float, float]] = deque()  # (open_time, quote_volume, close)
        self._qv_sum = 0.0
        self.last_1m: Bar | None = None

        self._ctx: LevelContext | None = None
        self._ctx_key: int | None = None

    # ------------------------------------------------------------------ updates
    def on_bar(self, bar: Bar) -> bool:
        """Feed a closed 1m bar. Returns True when it completed an entry-timeframe bar."""
        if self.last_1m is not None and bar.open_time <= self.last_1m.open_time:
            return False  # duplicate / out of order
        self.last_1m = bar
        self._day.append((bar.open_time, bar.quote_volume, bar.close))
        self._qv_sum += bar.quote_volume
        cutoff = bar.open_time - DAY_MS
        while self._day and self._day[0][0] <= cutoff:
            self._qv_sum -= self._day.popleft()[1]

        # Timeframes can coincide (e.g. entry 5m = scanner 5m); whichever aggregator delivers a bar
        # first processes it for every role, so collect the entry signal from all of them.
        entry_done = False
        for agg, tf in ((self.agg_scan, FIVE_MIN_MS), (self.agg_struct, self.struct_ms),
                        (self.agg_entry, self.entry_ms)):
            for b in agg.update(bar):
                entry_done = self._ingest(b, tf) or entry_done
        return entry_done

    def warmup_bar(self, bar: Bar) -> None:
        """Ingest a bar of any interval: 1m goes through `on_bar`, others directly to their TF."""
        if bar.interval_ms == MINUTE_MS:
            self.on_bar(bar)
        else:
            self._ingest(bar, bar.interval_ms)

    def _ingest(self, b: Bar, tf: int) -> bool:
        if b.open_time <= self._last_open.get(tf, -1):
            return False  # already have this bar (e.g. from REST warm-up)
        self._last_open[tf] = b.open_time
        # One timeframe can play several roles (e.g. 5m = scanner TF and structure TF).
        if tf == FIVE_MIN_MS:
            self.scan_atr.update(b.high, b.low, b.close)
        if tf == self.struct_ms:
            self._on_struct(b)
        if tf == self.entry_ms:
            self._on_entry(b)
            return True
        return False

    def _on_entry(self, b: Bar) -> None:
        self.last_bar = b
        self.bars_e.append(b)
        self.rsi_prev = self.rsi_now
        self.rsi_now = self.rsi.update(b.close)
        if self.rsi_now is not None:
            self.rsi_hist.append(self.rsi_now)
        self.atr_e.update(b.high, b.low, b.close)

    def _on_struct(self, b: Bar) -> None:
        self.atr_s.update(b.high, b.low, b.close)
        k = 2 / (self.cfg.trend_ema_period + 1)
        self.ema = b.close if self.ema is None else self.ema + k * (b.close - self.ema)
        self._ema_n += 1
        self._ema_hist.append(self.ema)
        self.t_s.append(b.open_time)
        self.h_s.append(b.high)
        self.l_s.append(b.low)
        self.c_s.append(b.close)
        c = self.cfg
        window = c.pivot_left + c.pivot_right + 1
        if len(self.h_s) >= window:
            tail_t = list(self.t_s)[-window:]
            tail_h = list(self.h_s)[-window:]
            tail_l = list(self.l_s)[-window:]
            self.pivots.extend(latest_pivots(tail_t, tail_h, tail_l, c.pivot_left, c.pivot_right))
        oldest = self.t_s[0]
        while self.pivots and self.pivots[0].ts < oldest:
            self.pivots.popleft()

    # ------------------------------------------------------------------ queries
    @property
    def ema_slope(self) -> float | None:
        """Change of the structure-TF EMA over `trend_slope_bars` bars (None until settled)."""
        if self._ema_n < self.cfg.trend_ema_period or len(self._ema_hist) < self._ema_hist.maxlen:
            return None
        return self._ema_hist[-1] - self._ema_hist[0]

    @property
    def ready(self) -> bool:
        return (
            self.rsi_now is not None
            and self.atr_e.value is not None
            and self.atr_s.value is not None
            and len(self.c_s) >= self.cfg.pivot_left + self.cfg.pivot_right + 10
        )

    @property
    def quote_volume_24h(self) -> float:
        return self._qv_sum

    @property
    def has_24h_history(self) -> bool:
        return bool(self._day) and self._day[-1][0] - self._day[0][0] >= DAY_MS - 2 * MINUTE_MS

    @property
    def change_24h(self) -> float | None:
        """Fractional price change over the last 24h (needs a full day of 1m bars)."""
        if not self.has_24h_history or self._day[0][2] <= 0:
            return None
        return self._day[-1][2] / self._day[0][2] - 1

    @property
    def volatility(self) -> float | None:
        """Mean 5m true range over the scanner window, as a fraction of price."""
        return self.scan_atr.value

    def level_context(self) -> LevelContext | None:
        if not self.ready or not self.t_s:
            return None
        key = self.t_s[-1]
        if self._ctx_key == key and self._ctx is not None:
            return self._ctx
        c = self.cfg
        atr = float(self.atr_s.value)  # type: ignore[arg-type]
        pivots = list(self.pivots)
        zones = build_zones(pivots, c.zone_tolerance_atr * atr, c.zone_min_touches)
        times, closes = list(self.t_s), list(self.c_s)
        kw = dict(lookback=c.trendline_lookback_pivots,
                  break_tol=c.trendline_break_tolerance_atr * atr,
                  touch_tol=c.zone_tolerance_atr * atr)
        sup = find_trendline(pivots, "support", times, closes, **kw)
        res = find_trendline(pivots, "resistance", times, closes, **kw)
        channel = impulse = None
        if c.name == "confluence":
            if c.use_channels:
                channel = find_channel(pivots, times, closes, c.channel_lookback_pivots,
                                       touch_tol=c.zone_tolerance_atr * atr,
                                       break_tol=c.trendline_break_tolerance_atr * atr * 3)
            if c.use_elliott:
                imp = find_impulse(pivots, c.elliott_strict_overlap)
                if imp is not None and key - imp.end.ts <= c.elliott_max_age_bars * self.struct_ms:
                    impulse = imp
        self._ctx = LevelContext(zones, sup, res, atr, closes[-1], times[-1], channel, impulse, tuple(pivots))
        self._ctx_key = key
        return self._ctx
