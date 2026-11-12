"""Confluence strategy: scoring, ladder geometry, stop beyond structure, target selection."""

import pytest

from autotr.config import Config
from autotr.indicators.channels import Channel
from autotr.indicators.elliott import Impulse
from autotr.indicators.levels import Zone
from autotr.indicators.pivots import Pivot
from autotr.market.features import LevelContext, SymbolState
from autotr.models import MINUTE_MS, Bar, Side
from autotr.strategy.confluence import ConfluenceStrategy

M5 = 5 * MINUTE_MS


def make_state(cfg, closes, lows, highs, rsi_hist, ctx, atr_e=0.2):
    st = SymbolState("SOLUSDT", cfg.strategy, cfg.scanner)
    for i, (c, lo, hi) in enumerate(zip(closes, lows, highs)):
        st.bars_e.append(Bar("SOLUSDT", i * M5, c, hi, lo, c, 1, c, M5))
    st.last_bar = st.bars_e[-1]
    st.rsi_hist.extend(rsi_hist)
    st.rsi_prev, st.rsi_now = rsi_hist[-2], rsi_hist[-1]
    st.atr_e.value = atr_e
    st.atr_s.value = ctx.atr
    st.c_s.extend([100.0] * 20)
    st.level_context = lambda: ctx  # type: ignore[method-assign]
    return st


def base_ctx(**kw):
    zones = [Zone(97.0, 97.3, 4, 0),          # strong support far below (stop reference)
             Zone(99.6, 99.9, 3, 0),          # support price is bouncing from
             Zone(103.0, 103.4, 2, 0)]        # resistance target
    args = dict(zones=zones, support_line=None, resistance_line=None, atr=1.0,
                last_close=100.2, last_open_time=0)
    args.update(kw)
    return LevelContext(**args)


def strat(cfg):
    return ConfluenceStrategy(cfg.strategy, cfg.exchange)


def test_long_at_strong_zone_scores_and_plans_ladder():
    cfg = Config()
    st = make_state(cfg, [100.3, 100.0, 100.4], [100.1, 99.85, 100.0], [100.6, 100.3, 100.5],
                    [35, 26, 31], base_ctx())
    sig = strat(cfg).on_bar(st)
    assert sig is not None and sig.side is Side.LONG
    # RSI 26 (+1) + turning (+0.5) + strong support zone x3 (+1.5) = 3.0
    assert "score 3:" in sig.reason and "support zone x3" in sig.reason
    prices = [p for p, _ in sig.ladder]
    assert prices[0] > prices[1] > prices[2]                     # stepping down toward the level
    assert prices[2] == pytest.approx(99.9)                      # deepest rung at the zone top
    assert [w for _, w in sig.ladder] == pytest.approx([0.2, 0.3, 0.5])
    # stop beyond the zone the setup relies on (low 99.6) minus 0.5*ATR(5m)
    assert sig.stop == pytest.approx(99.6 - 0.1)
    assert sig.take_profit == pytest.approx(103.0 - 0.02)         # just in front of resistance


def test_no_trade_below_min_score():
    cfg = Config()
    cfg.strategy.min_score = 3.5
    st = make_state(cfg, [100.3, 100.0, 100.4], [100.1, 99.85, 100.0], [100.6, 100.3, 100.5],
                    [35, 26, 31], base_ctx())
    assert strat(cfg).on_bar(st) is None


def test_rsi_gate_required():
    cfg = Config()
    st = make_state(cfg, [100.3, 100.0, 100.4], [100.1, 99.85, 100.0], [100.6, 100.3, 100.5],
                    [45, 40, 42], base_ctx())
    assert strat(cfg).on_bar(st) is None


def test_target_too_close_is_rejected():
    cfg = Config()
    ctx = base_ctx(zones=[Zone(97.0, 97.3, 4, 0), Zone(99.6, 99.9, 3, 0), Zone(100.8, 101.0, 2, 0)])
    st = make_state(cfg, [100.3, 100.0, 100.4], [100.1, 99.85, 100.0], [100.6, 100.3, 100.5],
                    [35, 26, 31], ctx)
    sig = strat(cfg).on_bar(st)
    # the first resistance at 100.8 pays too little; with no further level, no trade
    assert sig is None


def test_channel_floor_and_elliott_fade_add_points():
    cfg = Config()
    ch = Channel(t0=0, base=99.8, width=4.0, slope=0.0, touches_low=2, touches_high=2)
    pts = tuple(Pivot(i, p, k) for i, (p, k) in enumerate(
        [(110, "high"), (106, "low"), (108, "high"), (102, "low"), (104, "high"), (99.7, "low")]))
    imp = Impulse("down", pts, 1.0)
    ctx = base_ctx(zones=[Zone(97.0, 97.3, 4, 0), Zone(103.0, 103.4, 2, 0)], channel=ch, impulse=imp)
    st = make_state(cfg, [100.3, 100.0, 100.4], [100.1, 99.85, 100.0], [100.6, 100.3, 100.5],
                    [35, 26, 31], ctx)
    sig = strat(cfg).on_bar(st)
    assert sig is not None and sig.side is Side.LONG
    assert "channel floor +1" in sig.reason and "Elliott wave-5 fade" in sig.reason
    assert sig.stop == pytest.approx(99.7 - 0.1)                 # beyond the wave-5 low


def test_short_mirror_at_resistance():
    cfg = Config()
    zones = [Zone(96.5, 96.8, 2, 0), Zone(100.1, 100.4, 3, 0), Zone(102.7, 103.0, 4, 0)]
    ctx = base_ctx(zones=zones, last_close=99.8)
    st = make_state(cfg, [99.7, 100.0, 99.6], [99.4, 99.7, 99.5], [99.9, 100.15, 100.0],
                    [65, 74, 69], ctx)
    sig = strat(cfg).on_bar(st)
    assert sig is not None and sig.side is Side.SHORT
    prices = [p for p, _ in sig.ladder]
    assert prices[0] < prices[1] < prices[2] and prices[2] == pytest.approx(100.1)
    assert sig.stop > 100.4 and sig.take_profit == pytest.approx(96.8 + 0.02)
