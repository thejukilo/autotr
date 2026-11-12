import pytest

from autotr.config import Config
from autotr.indicators.levels import Zone
from autotr.market.features import LevelContext, SymbolState
from autotr.models import DAY_MS, MINUTE_MS, Bar, Side, Signal, Trade
from autotr.risk.manager import RiskManager
from autotr.sim.exchange import PaperExchange
from autotr.sim.filters import FilterBook
from autotr.strategy.scalper import ScalperStrategy


def make(cfg=None, balance=100.0):
    cfg = cfg or Config()
    ex = PaperExchange(cfg.exchange, balance)
    rm = RiskManager(cfg.risk, cfg.exchange, cfg.strategy, FilterBook())
    rm.update_clock(0, ex.equity())
    return cfg, ex, rm


def sig(sym="SOLUSDT", side=Side.LONG, price=100.0, stop=99.0, tp=101.5, ts=MINUTE_MS):
    return Signal(sym, side, ts, price, stop, tp, "test")


def test_position_size_risk_and_cap():
    cfg, ex, rm = make()
    # 1% stop: $2 risk / (1.00 stop + 0.14 fees+slippage per unit) = 1.755 units = $175.5 notional
    pe = rm.evaluate(sig(), ex)
    assert pe is not None
    assert pe.qty * 100 == pytest.approx(175.5, rel=0.01)
    assert pe.risk_usd == pytest.approx(2.0, rel=0.02)
    # 0.2% stop: risk-based size would be ~$700 notional, capped at 50% equity * 5x = $250
    cfg, ex, rm = make()
    pe = rm.evaluate(sig(stop=99.8, tp=100.3), ex)
    assert pe.qty * 100 <= 250.0 + 1e-9
    assert pe.risk_usd < 2.0


def test_guards():
    cfg, ex, rm = make()
    assert rm.evaluate(sig(stop=101), ex) is None  # stop on wrong side
    assert rm.rejections["invalid_levels"] == 1
    for i, s in enumerate(["AUSDT", "BUSDT"]):
        ex.submit_entry(rm.evaluate(sig(sym=s), ex))
    assert rm.evaluate(sig(sym="CUSDT"), ex) is None
    assert rm.rejections["max_positions"] == 1


def test_cooldown_and_daily_limit():
    cfg, ex, rm = make()
    rm.on_trade(Trade("SOLUSDT", Side.LONG, 1, 100, 99, 0, MINUTE_MS, -1, 0.1, 0, "stop_loss", 1))
    assert rm.evaluate(sig(ts=2 * MINUTE_MS), ex) is None and rm.rejections["cooldown"] == 1
    rm.update_clock(3 * MINUTE_MS, 93.0)  # -7% on the day
    assert rm.state.halted
    assert rm.evaluate(sig(sym="XUSDT", ts=10 * MINUTE_MS), ex) is None
    rm.update_clock(DAY_MS + 1, 93.0)  # next UTC day resets
    assert not rm.state.halted


def test_min_notional_rejection():
    # $10 account, 3% stop on BTC: $0.20 risk buys 0.00009 BTC, below the 0.001 lot / $100 minimum
    cfg, ex, rm = make(balance=10.0)
    s = sig(sym="BTCUSDT", price=75000, stop=72750, tp=78375)
    assert rm.evaluate(s, ex) is None and rm.rejections["below_min_notional"] == 1


# ---------------------------------------------------------------- strategy
def _state_with(bars, ctx, rsi_prev, rsi_now, atr1=0.2):
    cfg = Config()
    st = SymbolState("SOLUSDT", cfg.strategy, cfg.scanner)
    for b in bars:
        st.bars1.append(b)
    st.last_bar = bars[-1]
    st.rsi_prev, st.rsi_now = rsi_prev, rsi_now
    st.atr1.value = atr1
    st.level_context = lambda: ctx  # type: ignore[method-assign]
    return cfg, st


def _b(t, o, h, l, c):
    return Bar("SOLUSDT", t * MINUTE_MS, o, h, l, c, 1, c)


def test_long_signal_at_support_zone():
    bars = [_b(0, 100.6, 100.7, 100.1, 100.2), _b(1, 100.2, 100.3, 99.95, 100.1), _b(2, 100.1, 100.5, 100.0, 100.4)]
    ctx = LevelContext([Zone(99.8, 100.0, 3, 0), Zone(102.5, 102.8, 2, 0)], None, None,
                       atr5=0.5, last5_close=100.3, last5_open_time=0)
    cfg, st = _state_with(bars, ctx, rsi_prev=27, rsi_now=33)
    s = ScalperStrategy(cfg.strategy, cfg.exchange).on_bar(st)
    assert s is not None and s.side is Side.LONG
    assert s.stop == pytest.approx(99.8 - 0.5 * 0.2)
    assert s.take_profit == pytest.approx(100.4 + cfg.strategy.take_profit_r * (100.4 - 99.7))
    assert "support zone" in s.reason


def test_no_signal_without_rsi_cross_or_level():
    bars = [_b(0, 100, 100.2, 99.9, 100.1)] * 3
    ctx = LevelContext([Zone(95, 95.2, 3, 0)], None, None, atr5=0.5, last5_close=100, last5_open_time=0)
    cfg, st = _state_with(bars, ctx, rsi_prev=27, rsi_now=33)
    strat = ScalperStrategy(cfg.strategy, cfg.exchange)
    assert strat.on_bar(st) is None           # zone too far below
    st.rsi_prev, st.rsi_now = 40, 45
    assert strat.on_bar(st) is None           # no RSI trigger


def test_short_signal_rejected_when_target_zone_too_close():
    bars = [_b(0, 100, 100.6, 99.9, 100.4), _b(1, 100.4, 100.95, 100.3, 100.5), _b(2, 100.5, 100.6, 100.2, 100.3)]
    # resistance at 101; support zone just below at 100.1 caps the target -> poor R:R
    ctx = LevelContext([Zone(99.9, 100.1, 2, 0), Zone(101.0, 101.1, 3, 0)], None, None,
                       atr5=0.5, last5_close=100.4, last5_open_time=0)
    cfg, st = _state_with(bars, ctx, rsi_prev=74, rsi_now=68)
    assert ScalperStrategy(cfg.strategy, cfg.exchange).on_bar(st) is None
