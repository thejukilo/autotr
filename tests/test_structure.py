import pytest

from autotr.indicators.channels import find_channel
from autotr.indicators.elliott import find_impulse, zigzag
from autotr.indicators.pivots import Pivot

H = 3_600_000


def _channel_pivots(slope=0.5, width=10.0, base=100.0):
    # alternating touches of floor and ceiling every 5 hours
    pts = []
    for k in range(6):
        t = k * 5 * H
        if k % 2 == 0:
            pts.append(Pivot(t, base + slope * k * 5, "low"))
        else:
            pts.append(Pivot(t, base + width + slope * k * 5, "high"))
    return pts


def _series(pts, n_hours=30, slope=0.5, base=100.0, width=10.0):
    times = [h * H for h in range(n_hours)]
    closes = [base + slope * h + width / 2 for h in range(n_hours)]
    return times, closes


@pytest.mark.parametrize("slope", [0.5, -0.5, 0.0])
def test_channel_detected(slope):
    pts = _channel_pivots(slope)
    times, closes = _series(pts, slope=slope)
    ch = find_channel(pts, times, closes, lookback=20, touch_tol=0.3, break_tol=0.5)
    assert ch is not None
    assert ch.width == pytest.approx(10.0)
    assert ch.slope * H == pytest.approx(slope)
    assert ch.touches_low >= 2 and ch.touches_high >= 2
    assert ch.lower(25 * H) == pytest.approx(100 + slope * 25)
    assert ch.upper(25 * H) == pytest.approx(110 + slope * 25)


def test_channel_rejected_when_broken():
    pts = _channel_pivots(0.5)
    times, closes = _series(pts, slope=0.5)
    closes[20] = 90.0  # close far below the floor
    assert find_channel(pts, times, closes, lookback=20, touch_tol=0.3, break_tol=0.5) is None


def _impulse(prices, start_kind="low"):
    kinds = ["low", "high"] if start_kind == "low" else ["high", "low"]
    return [Pivot(i * H, p, kinds[i % 2]) for i, p in enumerate(prices)]


TEXTBOOK_UP = [100, 110, 104, 130, 122, 136]   # w1 10, w2 6 (60%), w3 26, w4 8 (31%), w5 14


def test_textbook_up_impulse():
    imp = find_impulse(_impulse(TEXTBOOK_UP))
    assert imp is not None and imp.direction == "up" and imp.fade_side == "SHORT"
    assert imp.quality == pytest.approx(1.0)
    assert imp.end.price == 136 and imp.wave4.price == 122


def test_down_impulse_mirrored():
    prices = [200 - (p - 100) for p in TEXTBOOK_UP]
    imp = find_impulse(_impulse(prices, "high"))
    assert imp is not None and imp.direction == "down" and imp.fade_side == "LONG"


@pytest.mark.parametrize("prices,rule", [
    ([100, 110, 99, 130, 122, 136], "wave 2 below start"),
    ([100, 110, 104, 109, 106, 120], "wave 3 not beyond wave 1"),
    ([100, 120, 110, 125, 121, 140], "wave 3 shortest"),
    ([100, 110, 104, 130, 108, 136], "wave 4 overlaps wave 1"),
    ([100, 110, 104, 130, 122, 129], "wave 5 truncated"),
])
def test_rule_violations_rejected(prices, rule):
    assert find_impulse(_impulse(prices)) is None, rule


def test_overlap_allowed_when_not_strict():
    assert find_impulse(_impulse([100, 110, 104, 130, 108, 136]), strict_overlap=False) is not None


def test_zigzag_keeps_extremes():
    pts = [Pivot(0, 100, "low"), Pivot(1, 110, "high"), Pivot(2, 115, "high"), Pivot(3, 105, "low"),
           Pivot(4, 103, "low"), Pivot(5, 120, "high")]
    zz = zigzag(pts)
    assert [(p.kind, p.price) for p in zz] == [("low", 100), ("high", 115), ("low", 103), ("high", 120)]
