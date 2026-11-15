"""Real-order path without any network or keys: request signing against Binance's documented vector,
and the full BinanceExchange order lifecycle against a stateful fake Binance over httpx.MockTransport.
"""

import httpx

from autotr.config import load_config
from autotr.data.binance_trade import BinanceTrade, sign
from autotr.models import Side
from autotr.sim.binance_exchange import BinanceExchange
from autotr.sim.exchange import PendingEntry, Rung
from autotr.sim.filters import FilterBook, SymbolFilters

SYM = "AAAUSDT"


def test_signing_is_correct_hmac_sha256():
    # RFC 4231 test case 2 — the canonical HMAC-SHA256 vector. Proves sign() uses the exact
    # construction Binance verifies against (SHA-256, hex digest), independent of any recollected value.
    assert sign(b"Jefe", "what do ya want for nothing?") == \
        "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"


class FakeBinance:
    """A tiny stateful futures account: one symbol, a position, and conditional orders."""

    def __init__(self):
        self.position_amt = 0.0
        self.entry_price = 0.0
        self.mark = 100.0
        self.orders: dict[int, dict] = {}
        self.next_id = 1000
        self.market_orders: list[dict] = []
        self.cancel_all_calls = 0

    def fill_limit(self, oid: int) -> None:
        """Simulate Binance filling a resting LIMIT rung: grow the position at its price."""
        o = self.orders[oid]
        qty, px = float(o["qty"]), float(o["price"])
        sgn = 1 if o["side"] == "BUY" else -1
        new_amt = self.position_amt + sgn * qty
        if self.position_amt and self.entry_price:
            self.entry_price = (self.entry_price * abs(self.position_amt) + px * qty) / abs(new_amt)
        else:
            self.entry_price = px
        self.position_amt = new_amt
        o["status"] = "FILLED"
        o["avgPrice"] = o["price"]

    def handler(self, req: httpx.Request) -> httpx.Response:
        path, p = req.url.path, dict(req.url.params)
        if path == "/fapi/v1/time":
            import time as _t
            return httpx.Response(200, json={"serverTime": int(_t.time() * 1000)})
        if path == "/fapi/v2/balance":
            return httpx.Response(200, json=[{"asset": "USDT", "balance": "100.0", "availableBalance": "100.0"}])
        if path in ("/fapi/v1/positionSide/dual", "/fapi/v1/marginType"):
            return httpx.Response(200, json={"code": 200, "msg": "success"})
        if path == "/fapi/v1/leverage":
            return httpx.Response(200, json={"leverage": int(p.get("leverage", 5)), "symbol": p.get("symbol")})
        if path == "/fapi/v2/positionRisk":
            return httpx.Response(200, json=[{"symbol": p.get("symbol", SYM),
                                              "positionAmt": str(self.position_amt),
                                              "entryPrice": str(self.entry_price), "markPrice": str(self.mark)}])
        if path == "/fapi/v1/order" and req.method == "POST":
            oid = self.next_id
            self.next_id += 1
            if p["type"] == "MARKET":
                self.market_orders.append(p)
                qty = float(p["quantity"])
                if p.get("reduceOnly") == "true":
                    self.position_amt, self.entry_price = 0.0, 0.0
                else:
                    self.position_amt = qty if p["side"] == "BUY" else -qty
                    self.entry_price = self.mark
                return httpx.Response(200, json={"orderId": oid, "status": "FILLED",
                                                 "executedQty": p["quantity"], "avgPrice": str(self.mark)})
            self.orders[oid] = {"type": p["type"], "status": "NEW", "stopPrice": p.get("stopPrice"),
                                "side": p.get("side"), "qty": p.get("quantity"), "price": p.get("price")}
            return httpx.Response(200, json={"orderId": oid, "status": "NEW"})
        if path == "/fapi/v1/order" and req.method == "GET":
            o = self.orders.get(int(p["orderId"]), {})
            return httpx.Response(200, json={"orderId": int(p["orderId"]), "status": o.get("status", "NEW"),
                                             "avgPrice": o.get("avgPrice", "0"), "stopPrice": o.get("stopPrice", "0")})
        if path == "/fapi/v1/order" and req.method == "DELETE":
            o = self.orders.get(int(p["orderId"]))
            if o and o["status"] == "NEW":
                o["status"] = "CANCELED"
            return httpx.Response(200, json={"orderId": int(p["orderId"]), "status": "CANCELED"})
        if path == "/fapi/v1/openOrders":
            return httpx.Response(200, json=[{"orderId": i, "type": o["type"], "stopPrice": o["stopPrice"]}
                                             for i, o in self.orders.items() if o["status"] == "NEW"])
        if path == "/fapi/v1/allOpenOrders" and req.method == "DELETE":
            self.cancel_all_calls += 1
            for o in self.orders.values():
                if o["status"] == "NEW":
                    o["status"] = "CANCELED"
            return httpx.Response(200, json={"code": 200, "msg": "success"})
        return httpx.Response(404, json={"code": -1, "msg": f"no route {req.method} {path}"})


def _build(fake: FakeBinance):
    cfg = load_config()
    cfg.account.starting_balance = 100.0
    filters = FilterBook({SYM: SymbolFilters(0.01, 0.001, 0.001, 5.0)})
    client = BinanceTrade("key", "secret", testnet=True, transport=httpx.MockTransport(fake.handler))
    ex = BinanceExchange(cfg, client, filters)
    ex.last_price[SYM] = 100.0
    return ex


def _entry() -> PendingEntry:
    return PendingEntry(symbol=SYM, side=Side.LONG, qty=1.0, stop=99.0, take_profit=101.0,
                        risk_usd=1.0, created_ts=1, time_stop_ms=10_000_000)


def test_preflight_adopts_balance_and_rejects_stray_position():
    fake = FakeBinance()
    ex = _build(fake)
    ex.preflight()
    assert ex.balance == 100.0

    fake.position_amt = 5.0  # a position the bot didn't open
    try:
        _build(fake).preflight()
        assert False, "preflight should refuse to start into a non-flat account"
    except RuntimeError as e:
        assert "not flat" in str(e)


def test_entry_opens_position_and_arms_stop_and_take_profit():
    fake = FakeBinance()
    ex = _build(fake)
    opened = []
    ex.on_open.append(opened.append)

    assert ex.submit_entry(_entry()) is True
    assert fake.position_amt == 1.0                       # real market order moved the position
    assert {o["type"] for o in fake.orders.values()} == {"STOP_MARKET", "TAKE_PROFIT_MARKET"}
    pos = ex.positions[SYM]
    assert pos.qty == 1.0 and pos.entry_price == 100.0 and opened == [pos]
    assert ex._orders[SYM]["stop_id"] and ex._orders[SYM]["tp_id"]


def test_reconcile_records_take_profit_close():
    fake = FakeBinance()
    ex = _build(fake)
    closed = []
    ex.on_trade.append(closed.append)
    ex.submit_entry(_entry())

    # Simulate Binance's take-profit firing: position flat, the TP order FILLED at 101.
    tp_id = ex._orders[SYM]["tp_id"]
    fake.orders[tp_id].update(status="FILLED", avgPrice="101")
    fake.position_amt = 0.0
    fake.mark = 101.0

    ex.on_quote(SYM, 100.99, 101.01, ts=5000)            # ts past the poll interval -> reconcile runs
    assert SYM not in ex.positions
    assert len(closed) == 1 and closed[0].exit_reason == "take_profit" and closed[0].exit_price == 101.0
    assert fake.cancel_all_calls >= 1                    # the other protective order was cancelled


def test_dry_run_places_no_orders():
    fake = FakeBinance()
    ex = _build(fake)
    ex.dry_run = True
    assert ex.submit_entry(_entry()) is False
    assert fake.position_amt == 0.0 and not fake.orders


def _ladder() -> PendingEntry:
    # Three resting limit rungs below the 100.0 mark; the stop/take-profit bracket the fill.
    return PendingEntry(symbol=SYM, side=Side.LONG, qty=3.0, stop=98.0, take_profit=102.0,
                        risk_usd=3.0, created_ts=1000, time_stop_ms=10_000_000,
                        rungs=[Rung(99.5, 1.0), Rung(99.0, 1.0), Rung(98.5, 1.0)],
                        expiry_ts=1000 + 3 * 60_000)


def test_ladder_rests_limit_rungs_then_opens_and_arms_on_first_fill():
    fake = FakeBinance()
    ex = _build(fake)
    opened = []
    ex.on_open.append(opened.append)

    assert ex.submit_entry(_ladder()) is True
    # three resting LIMIT rungs, no position and no protective orders yet
    limits = [o for o in fake.orders.values() if o["type"] == "LIMIT"]
    assert len(limits) == 3 and all(o["status"] == "NEW" for o in limits)
    assert SYM in ex.pending and SYM not in ex.positions
    assert not any(o["type"].endswith(("STOP_MARKET", "TAKE_PROFIT_MARKET")) for o in fake.orders.values())

    # Binance fills the first (shallowest) rung → position opens at its real price, exits armed.
    fake.fill_limit(min(fake.orders))
    ex.on_quote(SYM, 99.4, 99.6, ts=5000)                  # past the poll interval -> reconcile
    pos = ex.positions.get(SYM)
    assert pos is not None and pos.qty == 1.0 and pos.entry_price == 99.5 and opened == [pos]
    armed = {o["type"] for o in fake.orders.values()
             if o["status"] == "NEW" and o["type"] in ("STOP_MARKET", "TAKE_PROFIT_MARKET")}
    assert armed == {"STOP_MARKET", "TAKE_PROFIT_MARKET"}
    assert ex._orders[SYM]["stop_id"] and ex._orders[SYM]["tp_id"]


def test_ladder_grows_average_then_cancels_remainder_at_expiry():
    fake = FakeBinance()
    ex = _build(fake)
    ex.submit_entry(_ladder())
    ids = sorted(fake.orders)                              # the three rungs, in placement order

    fake.fill_limit(ids[0])                                # rung @ 99.5
    ex.on_quote(SYM, 99.4, 99.6, ts=5000)
    fake.fill_limit(ids[1])                                # rung @ 99.0
    ex.on_quote(SYM, 98.9, 99.1, ts=10_000)
    pos = ex.positions[SYM]
    assert pos.qty == 2.0 and pos.entry_price == 99.25 and pos.fills == 2

    # The ladder window closes with the third rung unfilled: it is cancelled, the position is kept,
    # and the symbol leaves the pending book. (cancel of a single rung, not cancel_all.)
    expiry = ex._entry_orders[SYM]["expiry"]
    ex.on_quote(SYM, 98.9, 99.1, ts=expiry + 1)
    assert fake.orders[ids[2]]["status"] == "CANCELED"
    assert SYM not in ex.pending and SYM in ex.positions
    assert fake.cancel_all_calls == 0                      # protective orders were never mass-cancelled


def test_ladder_that_never_fills_is_cancelled_at_expiry():
    fake = FakeBinance()
    ex = _build(fake)
    ex.submit_entry(_ladder())
    expiry = ex._entry_orders[SYM]["expiry"]
    ex.on_quote(SYM, 99.4, 99.6, ts=expiry + 1)            # window closed, no rung ever filled
    assert SYM not in ex.pending and SYM not in ex.positions
    assert all(o["status"] == "CANCELED" for o in fake.orders.values())
    assert ex.stats.entries_cancelled == 1
