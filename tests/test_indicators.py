import numpy as np
import pytest

from autotr.indicators.atr import ATR, RollingATRPct
from autotr.indicators.levels import build_zones, nearest_above, nearest_below
from autotr.indicators.pivots import Pivot, find_pivots, latest_pivots
from autotr.indicators.rsi import RSI, rsi
from autotr.indicators.trendlines import find_trendline
from autotr.market.bars import BarAggregator
from autotr.models import FIVE_MIN_MS, MINUTE_MS, Bar


# Classic Wilder worked example. With these 2-decimal closes the 14-period RSI is 70.46 at bar 15
# (published tables show 70.53 because they use the unrounded source prices).
CLOSES = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03,
          45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64]


def test_rsi_reference_values():
    out = rsi(np.array(CLOSES), 14)
    assert np.isnan(out[:14]).all()
    d = np.diff(CLOSES[:15])
    naive_first = 100 - 100 / (1 + d[d > 0].sum() / -d[d < 0].sum())
    assert out[14] == pytest.approx(naive_first)
    assert np.round(out[14:], 2).tolist() == [70.46, 66.25, 66.48, 69.35, 66.29, 57.92]


def test_rsi_extremes():
    up = RSI(3)
    vals = [up.update(float(x)) for x in range(10)]
    assert vals[-1] == 100.0
    flat = RSI(3)
    vals = [flat.update(5.0) for _ in range(10)]
    assert vals[-1] == 50.0


def test_atr_seed_and_smoothing():
    a = ATR(3)
    assert a.update(10, 8, 9) is None
    assert a.update(11, 9, 10) is None
    v = a.update(12, 10, 11)  # TRs: 2, 2, 2
    assert v == pytest.approx(2.0)
    v = a.update(15, 11, 14)  # TR 4 -> (2*2 + 4)/3
    assert v == pytest.approx(8 / 3)


def test_rolling_atr_pct():
    r = RollingATRPct(2)
    r.update(101, 99, 100)
    assert r.value is None
    r.update(102, 100, 100)  # TR = 2
    assert r.value == pytest.approx(2 / 100)


def test_pivots_found_and_streaming_matches_batch():
    highs = [1, 2, 3, 6, 3, 2, 1, 2, 3, 2, 1]
    lows = [h - 0.5 for h in highs]
    times = list(range(len(highs)))
    piv = find_pivots(times, highs, lows, 2, 2)
    assert Pivot(3, 6, "high") in piv
    assert Pivot(6, 0.5, "low") in piv
    streamed = []
    for n in range(1, len(highs) + 1):
        streamed += latest_pivots(times[:n], highs[:n], lows[:n], 2, 2)
    assert sorted(streamed, key=lambda p: (p.ts, p.kind)) == sorted(piv, key=lambda p: (p.ts, p.kind))


def test_zones_cluster_nearby_pivots():
    piv = [Pivot(1, 100.0, "low"), Pivot(2, 100.2, "high"), Pivot(3, 100.1, "low"),
           Pivot(4, 105.0, "high"), Pivot(5, 110.0, "high"), Pivot(6, 110.3, "high")]
    zones = build_zones(piv, tolerance=0.5, min_touches=2)
    assert len(zones) == 2
    assert zones[0].low == 100.0 and zones[0].high == 100.2 and zones[0].touches == 3
    assert zones[1].touches == 2
    assert nearest_below(zones, 104).low == 100.0
    assert nearest_above(zones, 104).low == 110.0
    assert nearest_below(zones, 99) is None


def test_support_trendline_and_break():
    # ascending lows at t=0 (100) and t=10 (105): line rises 0.5 per step
    piv = [Pivot(0, 100.0, "low"), Pivot(10, 105.0, "low")]
    times = list(range(0, 21))
    closes = [101.0 + 0.5 * t for t in times]
    line = find_trendline(piv, "support", times, closes, lookback=6, break_tol=0.01, touch_tol=0.1)
    assert line is not None and line.kind == "support"
    assert line.value_at(20) == pytest.approx(110.0)
    closes[15] = 100.0  # close well below the line -> invalid
    assert find_trendline(piv, "support", times, closes, lookback=6, break_tol=0.01, touch_tol=0.1) is None


def test_resistance_requires_descending_highs():
    piv = [Pivot(0, 100.0, "high"), Pivot(10, 101.0, "high")]
    assert find_trendline(piv, "resistance", [0, 10], [99, 99], 6, 0.1, 0.1) is None


def _bar(t, o, h, l, c, v=1.0):
    return Bar("X", t, o, h, l, c, v, v * c)


def test_bar_aggregator_5m():
    agg = BarAggregator(FIVE_MIN_MS)
    done = []
    for i in range(10):
        done += agg.update(_bar(i * MINUTE_MS, 10 + i, 11 + i, 9 + i, 10.5 + i))
    assert len(done) == 2
    b = done[0]
    assert (b.open_time, b.open, b.high, b.low, b.close) == (0, 10, 15, 9, 14.5)
    assert b.volume == 5


def test_bar_aggregator_flushes_on_gap():
    agg = BarAggregator(FIVE_MIN_MS)
    assert agg.update(_bar(0, 1, 2, 0.5, 1.5)) == []
    done = agg.update(_bar(6 * MINUTE_MS, 2, 3, 1, 2))  # minutes 1-4 missing
    assert len(done) == 1 and done[0].open_time == 0
