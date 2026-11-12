import pytest

from autotr.config import ExchangeCfg
from autotr.models import MINUTE_MS, Bar, Side
from autotr.sim.exchange import PaperExchange, PendingEntry, liquidation_price
from autotr.sim.filters import FilterBook, SymbolFilters, default_filters

CFG = ExchangeCfg(taker_fee=0.0005, maker_fee=0.0002, slippage_bps=0.0, leverage=5,
                  maintenance_margin_rate=0.01)


def bar(t, o, h, l, c, sym="X"):
    return Bar(sym, t * MINUTE_MS, o, h, l, c, 1.0, c)


def entry(side=Side.LONG, qty=2.0, stop=95.0, tp=110.0, time_stop_min=30):
    return PendingEntry("X", side, qty, stop, tp, risk_usd=10.0, created_ts=0, time_stop_ms=time_stop_min * MINUTE_MS)


def test_long_take_profit_accounting():
    ex = PaperExchange(CFG, 100.0)
    ex.submit_entry(entry())
    ex.on_bar(bar(1, 100, 101, 99, 100))
    pos = ex.positions["X"]
    assert pos.entry_price == 100 and pos.margin == pytest.approx(40.0)
    assert ex.balance == pytest.approx(100 - 200 * 0.0005)
    ex.on_bar(bar(2, 100, 111, 100, 109))
    assert "X" not in ex.positions
    t = ex.trades[0]
    assert t.exit_reason == "take_profit" and t.exit_price == 110
    assert t.gross_pnl == pytest.approx(20.0)
    assert t.fees == pytest.approx(200 * 0.0005 + 220 * 0.0002)
    assert ex.balance == pytest.approx(100 + t.net_pnl)


def test_stop_first_when_both_hit_in_same_bar():
    ex = PaperExchange(CFG, 100.0)
    ex.submit_entry(entry())
    ex.on_bar(bar(1, 100, 100, 100, 100))
    ex.on_bar(bar(2, 100, 120, 90, 100))
    assert ex.trades[0].exit_reason == "stop_loss" and ex.trades[0].exit_price == 95


def test_short_stop_and_gap_fill():
    ex = PaperExchange(CFG, 100.0)
    ex.submit_entry(entry(Side.SHORT, stop=105, tp=90))
    ex.on_bar(bar(1, 100, 100, 100, 100))
    ex.on_bar(bar(2, 107, 108, 106, 107))  # gaps over the stop -> filled at open
    t = ex.trades[0]
    assert t.exit_reason == "stop_loss (gap)" and t.exit_price == 107
    assert t.gross_pnl == pytest.approx(-14.0)


def test_entry_cancelled_if_open_beyond_stop():
    ex = PaperExchange(CFG, 100.0)
    ex.submit_entry(entry(stop=95))
    ex.on_bar(bar(1, 94, 96, 93, 95))
    assert not ex.positions and ex.stats.entries_cancelled == 1


def test_time_stop():
    ex = PaperExchange(CFG, 100.0)
    ex.submit_entry(entry(time_stop_min=3))
    for i in range(1, 6):
        ex.on_bar(bar(i, 100, 101, 99, 100.5))
    assert ex.trades and ex.trades[0].exit_reason == "time_stop"
    assert ex.trades[0].exit_ts == 4 * MINUTE_MS  # opened at t=1, 3 min later


def test_liquidation_price_formula_and_gap_liquidation():
    lp = liquidation_price(Side.LONG, 100, 5, 0.01)
    assert lp == pytest.approx(80 / 0.99)
    sp = liquidation_price(Side.SHORT, 100, 5, 0.01)
    assert sp == pytest.approx(120 / 1.01)
    ex = PaperExchange(CFG, 100.0)
    ex.submit_entry(entry(stop=70, tp=150))  # stop deliberately beyond liquidation
    ex.on_bar(bar(1, 100, 100, 100, 100))
    ex.on_bar(bar(2, 75, 76, 74, 75))
    t = ex.trades[0]
    assert t.exit_reason == "liquidation"
    assert ex.balance == pytest.approx(100 - 0.1 - 40.0)


def test_funding_sign():
    ex = PaperExchange(CFG, 100.0)
    ex.submit_entry(entry())
    ex.on_bar(bar(1, 100, 100, 100, 100))
    paid = ex.on_funding("X", 0.0001, 100.0, 2 * MINUTE_MS)
    assert paid == pytest.approx(-0.02)  # long pays positive funding
    ex.on_bar(bar(3, 100, 111, 100, 110))
    assert ex.trades[0].funding == pytest.approx(-0.02)
    assert ex.balance == pytest.approx(100 + ex.trades[0].net_pnl)


def test_quote_model_long():
    ex = PaperExchange(CFG, 100.0)
    ex.submit_entry(entry())
    ex.on_quote("X", 99.9, 100.0, 1)
    assert ex.positions["X"].entry_price == 100.0
    ex.on_quote("X", 104, 104.1, 2)
    assert "X" in ex.positions
    ex.on_quote("X", 94.9, 95.0, 3)
    assert ex.trades[0].exit_reason == "stop_loss" and ex.trades[0].exit_price == 94.9


def test_state_roundtrip():
    ex = PaperExchange(CFG, 100.0)
    ex.submit_entry(entry())
    ex.on_bar(bar(1, 100, 100, 100, 100))
    ex2 = PaperExchange(CFG, 0.0)
    ex2.load_state(ex.to_state())
    assert ex2.balance == ex.balance
    assert ex2.positions["X"].side is Side.LONG
    assert ex2.positions["X"].liq_price == ex.positions["X"].liq_price


def test_filters_rounding_and_defaults():
    f = SymbolFilters(0.01, 0.001, 0.001, 5.0)
    assert f.round_qty(1.23456) == 1.234
    assert f.round_price(10.006) == 10.01
    d = default_filters(0.2)
    assert d.step_size * 0.2 <= 0.5 and d.min_notional == 5.0
    fb = FilterBook()
    assert fb.get("BTCUSDT", 75000).min_notional == 100.0
    info = {"symbols": [{"symbol": "AAAUSDT", "filters": [
        {"filterType": "PRICE_FILTER", "tickSize": "0.0001"},
        {"filterType": "LOT_SIZE", "stepSize": "1", "minQty": "1"},
        {"filterType": "MARKET_LOT_SIZE", "stepSize": "1", "minQty": "1"},
        {"filterType": "MIN_NOTIONAL", "notional": "5"}]}]}
    parsed = FilterBook.parse_exchange_info(info)
    assert parsed["AAAUSDT"] == SymbolFilters(0.0001, 1.0, 1.0, 5.0)
