"""Laddered limit entries in the paper exchange and their sizing in the risk manager."""

import pytest

from autotr.config import Config, ExchangeCfg
from autotr.models import MINUTE_MS, Bar, Side, Signal
from autotr.report.tradelog import LiveStore
from autotr.risk.manager import RiskManager
from autotr.sim.exchange import PaperExchange, PendingEntry, Rung
from autotr.sim.filters import FilterBook

CFG = ExchangeCfg(taker_fee=0.0005, maker_fee=0.0002, slippage_bps=0.0, leverage=5, maintenance_margin_rate=0.01)


def bar(t, o, h, l, c, sym="X"):
    return Bar(sym, t * MINUTE_MS, o, h, l, c, 1.0, c)


def ladder(side=Side.LONG, prices=(99.0, 98.0, 97.0), qtys=(0.2, 0.3, 0.5), stop=95.0, tp=104.0,
           expiry_min=None, time_stop_min=600):
    return PendingEntry("X", side, 0, stop, tp, 1.0, 0, time_stop_min * MINUTE_MS,
                        rungs=[Rung(p, q) for p, q in zip(prices, qtys)],
                        expiry_ts=expiry_min * MINUTE_MS if expiry_min else None)


def test_partial_fills_average_and_maker_fees():
    ex = PaperExchange(CFG, 100.0)
    ex.submit_entry(ladder())
    ex.on_bar(bar(1, 99.5, 99.6, 98.5, 99.0))        # trades through 99 only
    pos = ex.positions["X"]
    assert pos.qty == pytest.approx(0.2) and pos.entry_price == 99.0 and pos.fills == 1
    assert ex.balance == pytest.approx(100 - 0.2 * 99 * 0.0002)
    ex.on_bar(bar(2, 98.8, 98.9, 96.9, 97.5))        # through 98 and 97
    pos = ex.positions["X"]
    assert pos.qty == pytest.approx(1.0) and pos.fills == 3
    assert pos.entry_price == pytest.approx((0.2 * 99 + 0.3 * 98 + 0.5 * 97) / 1.0)
    assert "X" not in ex.pending                       # ladder fully used
    ex.on_bar(bar(3, 100, 104.5, 99.9, 104.2))        # take profit
    t = ex.trades[0]
    assert t.exit_reason == "take_profit" and t.fills == 3
    assert ex.balance == pytest.approx(100 + t.net_pnl)


def test_no_take_profit_in_bar_that_filled_a_rung():
    ex = PaperExchange(CFG, 100.0)
    ex.submit_entry(ladder())
    ex.on_bar(bar(1, 99.5, 105, 98.9, 104))           # fills 99 and would touch TP: not both
    assert "X" in ex.positions and not ex.trades


def test_cancel_when_target_reached_before_fill():
    ex = PaperExchange(CFG, 100.0)
    ex.submit_entry(ladder())
    ex.on_bar(bar(1, 100, 104.5, 99.5, 104))
    assert not ex.positions and not ex.pending and ex.stats.entries_cancelled == 1


def test_stop_after_one_rung_cancels_rest():
    ex = PaperExchange(CFG, 100.0)
    ex.submit_entry(ladder(prices=(99.0, 96.0, 95.5), stop=95.0))
    ex.on_bar(bar(1, 99.5, 99.6, 98.9, 99.2))         # first rung
    ex.on_bar(bar(2, 97, 97, 94.0, 94.5))             # skips through 96/95.5 and the stop in one bar
    t = ex.trades[0]
    assert t.exit_reason == "stop_loss" and not ex.pending
    assert t.fills == 3  # deeper rungs filled on the way down, then the stop hit (conservative)


def test_expiry_cancels_unfilled_but_keeps_position():
    ex = PaperExchange(CFG, 100.0)
    ex.submit_entry(ladder(expiry_min=4))
    ex.on_bar(bar(1, 99.5, 99.6, 98.9, 99.2))
    ex.on_bar(bar(2, 99.2, 99.4, 99.1, 99.3))
    assert "X" in ex.pending
    ex.on_bar(bar(3, 99.3, 99.4, 99.1, 99.3))         # close_time = 4 min >= expiry 4 min
    assert "X" not in ex.pending and ex.positions["X"].qty == pytest.approx(0.2)


def test_expiry_without_fill_cancels_plan():
    ex = PaperExchange(CFG, 100.0)
    ex.submit_entry(ladder(expiry_min=2))
    ex.on_bar(bar(1, 100, 100.5, 99.5, 100))
    ex.on_bar(bar(2, 100, 100.5, 99.5, 100))
    assert not ex.pending and ex.stats.entries_cancelled == 1


def test_short_ladder_and_quotes():
    ex = PaperExchange(CFG, 100.0)
    ex.submit_entry(ladder(Side.SHORT, prices=(101.0, 102.0, 103.0), stop=105.0, tp=96.0))
    ex.on_quote("X", 100.9, 101.0, 1)                 # bid 100.9 < 101: sell limit not hit
    assert "X" not in ex.positions
    ex.on_quote("X", 101.1, 101.2, 2)                 # bid reaches 101.1 ≥ 101: filled at 101
    assert ex.positions["X"].entry_price == 101.0
    ex.on_quote("X", 102.2, 102.3, 3)
    assert ex.positions["X"].fills == 2
    ex.on_quote("X", 95.8, 95.9, 4)                   # ask 95.9 ≤ 96: take profit
    assert ex.trades[0].exit_reason == "take_profit" and not ex.pending


def test_ladder_state_roundtrip_and_margin_reservation():
    ex = PaperExchange(CFG, 100.0)
    ex.submit_entry(ladder())
    assert ex.available_balance() == pytest.approx(100 - (0.2 * 99 + 0.3 * 98 + 0.5 * 97) / 5)
    ex.on_bar(bar(1, 99.5, 99.6, 98.5, 99.0))
    ex2 = PaperExchange(CFG, 0)
    ex2.load_state(ex.to_state())
    assert [r.filled for r in ex2.pending["X"].rungs] == [True, False, False]
    assert ex2.positions["X"].fills == 1


def test_risk_manager_ladder_sizing():
    cfg = Config()
    ex = PaperExchange(cfg.exchange, 100.0)
    rm = RiskManager(cfg.risk, cfg.exchange, cfg.strategy, FilterBook())
    rm.update_clock(0, 100.0)
    sig = Signal("SOLUSDT", Side.LONG, 0, 100.0, 96.0, 108.0, "t",
                 ladder=[(99.5, 0.2), (98.75, 0.3), (98.0, 0.5)])
    pe = rm.evaluate(sig, ex)
    assert pe is not None and len(pe.rungs) == 3 and all(r.price for r in pe.rungs)
    assert pe.risk_usd == pytest.approx(2.0, rel=0.02)          # full fill risks 2% of equity
    q = [r.qty for r in pe.rungs]
    assert q[0] < q[1] < q[2]                                    # biggest rung closest to the level
    assert pe.expiry_ts == cfg.strategy.ladder_expiry_bars * 5 * MINUTE_MS


def test_live_store_keeps_ladder_fields(tmp_path):
    ex = PaperExchange(CFG, 100.0)
    store = LiveStore(tmp_path / "t.db")
    ex.on_trade.append(store.add_trade)
    ex.submit_entry(ladder())
    ex.on_bar(bar(1, 99.5, 99.6, 97.9, 98.5))
    ex.on_bar(bar(2, 100, 104.5, 99.9, 104.2))
    (t,) = store.trades()
    assert t.fills == 2 and t.stop == 95.0 and t.take_profit == 104.0
