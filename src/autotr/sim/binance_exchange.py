"""Real-money Binance USDT-M futures exchange — a drop-in for PaperExchange that places REAL orders.

Binance is the source of truth. An accepted entry is sent as a real MARKET order; its stop and
take-profit are immediately placed as reduce-only `closePosition` orders ON BINANCE, so they protect
the position even if this bot crashes or loses its connection. A poll loop (`_reconcile`) compares
local state to the real account every few seconds and records a Trade when a position has been closed
on the exchange (by the stop, the take-profit, or liquidation); it also arms the time-stop and keeps
a trailed stop in sync by cancel/replace.

It subclasses PaperExchange to reuse all the *accounting* and *persistence* the engine, risk manager
and dashboard depend on — `balance`, `positions`, `pending`, `equity()`, `available_balance()`,
`to_state()`/`load_state()` — while `balance` is kept equal to the real futures wallet. Only the
methods that *simulate* fills are overridden to hit the exchange.

SCOPE:
  * Entry mirrors the backtest. A strategy's limit ladder is placed as real post-only (maker) LIMIT
    rungs that rest on the book; the position opens as those rungs fill (detected by the poll loop),
    at the real average entry Binance reports. A market strategy (`market: true`, a single price-less
    rung) still sends one MARKET order. Unfilled rungs are cancelled when the ladder window expires.
  * Exits are server-side STOP_MARKET + TAKE_PROFIT_MARKET with closePosition=true, armed as soon as
    the first rung fills — so they always close the WHOLE position even as deeper rungs fill, and
    protect it even if this bot dies. Time-stop and trailing stop are driven by the poll loop.
  * Requires one-way position mode and isolated margin (both set at preflight / first use).
  * Trade.net_pnl in the log is an estimate; the account's `balance` is read from the real wallet and
    is exact — the dashboard equity curve uses that.
  * A resting ladder is NOT resumed across a restart: on resume any un-filled entry orders are
    cancelled (see resync_after_resume), so the bot never re-adopts an order it can't track.

Validate on TESTNET before using real funds. See the run banner.
"""

from __future__ import annotations

import logging
import time

from autotr.config import Config
from autotr.data.binance_trade import BinanceError, BinanceTrade
from autotr.models import Side, Trade
from autotr.sim.exchange import PaperExchange, PendingEntry, Position, liquidation_price
from autotr.sim.filters import FilterBook

log = logging.getLogger("autotr.live")


def _now_ms() -> int:
    return int(time.time() * 1000)


class BinanceExchange(PaperExchange):
    def __init__(self, cfg: Config, client: BinanceTrade, filters: FilterBook):
        super().__init__(cfg.exchange, cfg.account.starting_balance)
        self.client = client
        self.filters = filters
        self.live = cfg.live
        self.dry_run = cfg.live.dry_run
        self.max_notional = cfg.live.max_notional_usd
        self._orders: dict[str, dict] = {}        # symbol -> {stop_id, tp_id, stop_px, exit_side}
        self._entry_orders: dict[str, dict] = {}   # symbol -> resting ladder (orders, placed_qty, expiry, ...)
        self._poll_ms = int(cfg.live.poll_seconds * 1000)
        self._last_poll = 0
        self._configured: set[str] = set()         # symbols whose leverage/margin we've set this run

    # ------------------------------------------------------------ startup
    def preflight(self) -> None:
        """Prove the keys work and the account is in the mode we require, and adopt the real balance.
        Raises on any pre-existing open position/orders so the bot never starts into an unknown book."""
        self.client.sync_time()                    # align to Binance's clock before any signed call
        wallet, avail = self.client.wallet_usdt()
        self.balance = wallet
        log.info("live account: wallet $%.2f, available $%.2f (%s)", wallet, avail,
                 "TESTNET" if self.client.testnet else "MAINNET — REAL FUNDS")
        self.client.set_one_way_mode()
        stray = [p for p in self.client.position_risk() if abs(float(p.get("positionAmt", 0))) > 0]
        orders = self.client.open_orders()
        if (stray or orders) and not self.positions:
            syms = sorted({p["symbol"] for p in stray} | {o["symbol"] for o in orders})
            raise RuntimeError(
                "Binance futures account is not flat: open positions/orders on "
                f"{', '.join(syms)}. Close them (or cancel the orders) before starting the bot, so it "
                "begins from a known state. The bot only manages positions it opened itself.")

    def resync_after_resume(self, ts: int) -> None:
        """After restoring positions from the DB, reconcile them with Binance: drop any that already
        closed, rebuild protective-order ids from the live book, and re-arm anything missing so no
        restored position is ever left unprotected."""
        self._sync_balance()
        try:
            risks = {p["symbol"]: p for p in self.client.position_risk()}
        except BinanceError:
            log.exception("resume resync failed — could not read positions; starting cautiously")
            return
        for symbol, pos in list(self.positions.items()):
            if abs(float(risks.get(symbol, {}).get("positionAmt", 0.0))) < 1e-12:
                log.warning("resumed position %s is already closed on Binance — recording it", symbol)
                self._orders.pop(symbol, None)
                self._finalize_close(symbol, pos, self.last_price.get(symbol, pos.entry_price), ts, "exit")
                continue
            stop_id = tp_id = None
            try:
                for o in self.client.open_orders(symbol):
                    if o["type"] == "STOP_MARKET":
                        stop_id = o["orderId"]
                    elif o["type"] == "TAKE_PROFIT_MARKET":
                        tp_id = o["orderId"]
            except BinanceError:
                log.exception("could not read open orders for resumed %s", symbol)
            self._orders[symbol] = {"stop_id": stop_id, "tp_id": tp_id,
                                    "stop_px": self.filters.get(symbol, pos.entry_price).round_price(pos.stop),
                                    "exit_side": self._exit_side(pos.side)}
            if stop_id is None or tp_id is None:
                log.warning("resumed %s missing a protective order — re-arming both", symbol)
                try:
                    self.client.cancel_all(symbol)
                    self._orders[symbol] = self._arm_exits(
                        symbol, pos.side, pos.qty,
                        self.filters.get(symbol, pos.entry_price).round_price(pos.stop),
                        self.filters.get(symbol, pos.entry_price).round_price(pos.take_profit))
                except BinanceError:
                    log.exception("could not re-arm exits for %s", symbol)
        extra = [s for s, p in risks.items() if abs(float(p.get("positionAmt", 0.0))) > 0
                 and s not in self.positions]
        if extra:
            log.warning("Binance has position(s) the bot did not open: %s — leaving them untouched",
                        ", ".join(extra))
        # A resting ladder can't be reliably resumed (its order ids aren't persisted): cancel any
        # un-filled entry limit orders and drop the plan, so the bot never re-adopts one it can't track.
        for symbol in list(self.pending):
            if symbol in self.positions:
                continue  # already a live position; its protective orders are handled above
            log.warning("dropping un-resumable resting ladder for %s and cancelling its orders", symbol)
            try:
                for o in self.client.open_orders(symbol):
                    if o.get("type") == "LIMIT":
                        self.client.cancel(symbol, o["orderId"])
            except BinanceError:
                log.exception("could not cancel resting orders for %s", symbol)
            self.pending.pop(symbol, None)
            self._entry_orders.pop(symbol, None)

    # ------------------------------------------------------------ order helpers
    def _exit_side(self, side: Side) -> str:
        return "SELL" if side is Side.LONG else "BUY"

    def _configure(self, symbol: str) -> None:
        if symbol in self._configured:
            return
        self.client.set_isolated(symbol)
        self.client.set_leverage(symbol, self.cfg.leverage)
        self._configured.add(symbol)

    def _arm_exits(self, symbol: str, side: Side, qty: float, stop_px: float, tp_px: float) -> dict:
        """Place the reduce-only stop and take-profit. If either fails, flatten and re-raise."""
        exit_side = self._exit_side(side)
        stop_id = tp_id = None
        try:
            stop_id = self.client.stop_market(symbol, exit_side, stop_px).get("orderId")
            tp_id = self.client.take_profit_market(symbol, exit_side, tp_px).get("orderId")
        except BinanceError:
            log.exception("PROTECTIVE ORDER FAILED for %s — flattening position for safety", symbol)
            try:
                self.client.cancel_all(symbol)
                self.client.market(symbol, exit_side, qty, reduce_only=True)
            except BinanceError:
                log.exception("emergency flatten ALSO failed for %s — CHECK THE ACCOUNT MANUALLY", symbol)
            raise
        return {"stop_id": stop_id, "tp_id": tp_id, "stop_px": stop_px, "exit_side": exit_side}

    # ------------------------------------------------------------ entries (override)
    def submit_entry(self, pe: PendingEntry) -> bool:
        """Place an entry. A ladder (limit rungs) rests on the book and opens the position as it
        fills; a market rung opens immediately. Returns True if orders were placed."""
        if self.busy(pe.symbol):
            return False
        ref = self.last_price.get(pe.symbol) or next((r.price for r in pe.rungs if r.price), 0.0) or 0.0
        f = self.filters.get(pe.symbol, ref)
        stop_px, tp_px = f.round_price(pe.stop), f.round_price(pe.take_profit)
        limit_rungs = [r for r in pe.rungs if r.price is not None]
        if self.dry_run:
            plan = (", ".join(f"{f.round_qty(r.qty):g}@{f.round_price(r.price):.6g}" for r in limit_rungs)
                    if limit_rungs else f"{f.round_qty(pe.qty):g}@market")
            log.info("[DRY-RUN] would open %s %s  [%s]  stop %.6g  tp %.6g",
                     pe.side.name, pe.symbol, plan, stop_px, tp_px)
            return False
        if not limit_rungs:
            return self._market_entry(pe, f, ref, stop_px, tp_px)
        return self._ladder_entry(pe, f, stop_px, tp_px)

    def _open_position(self, pe: PendingEntry, qty: float, avg: float, stop_px: float, tp_px: float,
                       orders: dict, entry_fee_rate: float, fills: int) -> Position:
        """Build the Position and register it (shared by the market and ladder entry paths)."""
        now = _now_ms()
        cost = self.cfg.taker_fee + self.slip
        risk = qty * (abs(avg - stop_px) + avg * entry_fee_rate + stop_px * cost)
        pos = Position(pe.symbol, pe.side, qty, avg, now, stop_px, tp_px,
                       qty * avg / self.cfg.leverage,
                       liquidation_price(pe.side, avg, self.cfg.leverage, self.cfg.maintenance_margin_rate),
                       qty * avg * entry_fee_rate, risk, now + pe.time_stop_ms, pe.reason,
                       signal_ts=pe.created_ts, confidence=pe.confidence,
                       rungs=tuple(r.price for r in pe.rungs if r.price is not None), fills=fills)
        self.positions[pe.symbol] = pos
        self._orders[pe.symbol] = orders
        self.stats.entries_filled += 1
        self.stats.rungs_filled += fills
        for cb in self.on_open:
            cb(pos)
        return pos

    def _market_entry(self, pe: PendingEntry, f, ref: float, stop_px: float, tp_px: float) -> bool:
        qty = f.round_qty(pe.qty)
        if qty < f.min_qty or (ref and qty * ref < f.min_notional):
            log.info("skip %s: qty %g below exchange minimum", pe.symbol, qty)
            return False
        if self.max_notional and ref and qty * ref > self.max_notional:
            log.warning("skip %s: notional $%.2f over cap $%.2f", pe.symbol, qty * ref, self.max_notional)
            return False
        side = "BUY" if pe.side is Side.LONG else "SELL"
        try:
            self._configure(pe.symbol)
            resp = self.client.market(pe.symbol, side, qty)
        except BinanceError:
            log.exception("entry order rejected for %s", pe.symbol)
            return False
        filled = float(resp.get("executedQty") or 0.0)
        avg = float(resp.get("avgPrice") or 0.0) or ref
        if filled <= 0 or avg <= 0:
            log.error("entry for %s did not fill cleanly: %s", pe.symbol, resp)
            return False
        try:
            orders = self._arm_exits(pe.symbol, pe.side, filled, stop_px, tp_px)
        except BinanceError:
            return False  # position already flattened in _arm_exits
        self._open_position(pe, filled, avg, stop_px, tp_px, orders, self.cfg.taker_fee, fills=1)
        self._sync_balance()
        return True

    def _expiry_ms(self, pe: PendingEntry) -> int:
        """How long the resting ladder lives (mirrors the backtest's ladder_expiry window)."""
        if pe.expiry_ts and pe.created_ts and pe.expiry_ts > pe.created_ts:
            return pe.expiry_ts - pe.created_ts
        return 15 * 60 * 1000

    def _ladder_entry(self, pe: PendingEntry, f, stop_px: float, tp_px: float) -> bool:
        """Rest the strategy's limit rungs on the book as post-only (maker) orders. The position is
        opened later, by _poll_entry, as Binance fills them. Exits are armed on the first fill."""
        side = "BUY" if pe.side is Side.LONG else "SELL"
        try:
            self._configure(pe.symbol)
        except BinanceError:
            log.exception("could not set leverage/margin for %s — skipping entry", pe.symbol)
            return False
        placed: list[dict] = []
        placed_qty = 0.0
        for r in pe.rungs:
            if r.price is None:
                continue
            price = f.round_price(r.price)
            qty = f.round_qty(r.qty)
            if qty < f.min_qty or qty * price < f.min_notional:
                continue
            if self.max_notional and (placed_qty + qty) * price > self.max_notional:
                break  # per-position notional cap reached across the ladder
            try:
                resp = self.client.limit(pe.symbol, side, qty, price, post_only=True)
            except BinanceError:
                log.exception("ladder rung rejected for %s %g @ %.6g", pe.symbol, qty, price)
                continue
            placed.append({"id": resp.get("orderId"), "qty": qty, "price": price})
            placed_qty += qty
        if not placed:
            log.info("skip %s: no ladder rung could be placed", pe.symbol)
            return False
        self.pending[pe.symbol] = pe
        self._entry_orders[pe.symbol] = {
            "pe": pe, "orders": placed, "placed_qty": placed_qty, "filled_qty": 0.0,
            "stop_px": stop_px, "tp_px": tp_px, "exit_side": self._exit_side(pe.side),
            "expiry": _now_ms() + self._expiry_ms(pe),
        }
        log.info("laddered %s %s: %d rungs resting, %.6g qty total  stop %.6g  tp %.6g",
                 side, pe.symbol, len(placed), placed_qty, stop_px, tp_px)
        return True

    # ------------------------------------------------------------ price events (override)
    def on_quote(self, symbol: str, bid: float, ask: float, ts: int) -> None:
        self.last_price[symbol] = (bid + ask) / 2
        if ts - self._last_poll >= self._poll_ms:
            self._last_poll = ts
            self._reconcile(ts)

    def on_bar(self, bar) -> None:            # live uses bar_fills=False; never simulate on real
        self.last_price[bar.symbol] = bar.close

    def on_funding(self, symbol: str, rate: float, mark_price: float, ts: int) -> float:
        return 0.0                            # real funding is applied by Binance; picked up by _sync_balance

    def close_market(self, symbol: str, price: float, ts: int, reason: str = "manual") -> Trade | None:
        pos = self.positions.get(symbol)
        if pos is None:
            return None
        return self._close_now(symbol, pos, ts, reason)

    # ------------------------------------------------------------ reconciliation
    def _sync_balance(self) -> None:
        try:
            wallet, _ = self.client.wallet_usdt()
            self.balance = wallet
        except BinanceError:
            log.warning("could not refresh wallet balance", exc_info=True)

    def _reconcile(self, ts: int) -> None:
        try:
            risks = {p["symbol"]: p for p in self.client.position_risk()}
        except BinanceError:
            log.warning("positionRisk poll failed", exc_info=True)
            return
        for symbol in list(self._entry_orders):                   # resting ladders → open as they fill
            self._poll_entry(symbol, risks.get(symbol), ts)
        for symbol, pos in list(self.positions.items()):
            r = risks.get(symbol)
            amt = float(r.get("positionAmt", 0.0)) if r else 0.0
            if r and r.get("markPrice"):
                self.last_price[symbol] = float(r["markPrice"])
            if abs(amt) < 1e-12:
                self._record_exchange_close(symbol, pos, ts)      # stop/tp/liq fired on Binance
                continue
            self._sync_stop(symbol, pos)                          # trailed stop → cancel/replace
            if ts >= pos.time_stop_ts:
                self._close_now(symbol, pos, ts, "time_stop")
        self._sync_balance()

    def _poll_entry(self, symbol: str, r: dict | None, ts: int) -> None:
        """Open/grow a position as its resting ladder fills, and arm exits on the first fill. Cancels
        any un-filled rungs once the ladder is complete or its window has expired."""
        eo = self._entry_orders.get(symbol)
        pe = self.pending.get(symbol)
        if eo is None or pe is None:
            self._entry_orders.pop(symbol, None)
            return
        amt = abs(float(r.get("positionAmt", 0.0))) if r else 0.0
        entry_px = float(r.get("entryPrice", 0.0)) if r else 0.0
        if r and r.get("markPrice"):
            self.last_price[symbol] = float(r["markPrice"])
        if amt > 1e-12:
            pos = self.positions.get(symbol)
            if pos is None:                                       # first fill: open + arm server exits
                try:
                    orders = self._arm_exits(symbol, pe.side, amt, eo["stop_px"], eo["tp_px"])
                except BinanceError:
                    self._fail_entry(symbol)                      # _arm_exits already flattened
                    return
                self._open_position(pe, amt, entry_px or eo["orders"][0]["price"],
                                    eo["stop_px"], eo["tp_px"], orders, self.cfg.maker_fee, fills=1)
            elif amt > pos.qty + 1e-12:                           # a deeper rung filled: update average
                pos.fills += 1
                pos.qty = amt
                if entry_px > 0:
                    pos.entry_price = entry_px
                    pos.margin = amt * entry_px / self.cfg.leverage
                self.stats.rungs_filled += 1
            eo["filled_qty"] = amt
        if amt >= eo["placed_qty"] - 1e-9 or ts >= eo["expiry"]:  # fully filled, or window closed
            self._settle_entry(symbol, eo, filled=amt > 1e-12)

    def _settle_entry(self, symbol: str, eo: dict, filled: bool) -> None:
        """Cancel still-resting rungs individually (never cancel_all — that would kill the armed
        protective orders) and clear the ladder from the pending book."""
        for o in eo["orders"]:
            oid = o.get("id")
            if oid is None:
                continue
            try:
                st = self.client.order_status(symbol, oid)
                if st.get("status") in ("NEW", "PARTIALLY_FILLED"):
                    self.client.cancel(symbol, oid)
            except BinanceError:
                pass
        self._entry_orders.pop(symbol, None)
        self.pending.pop(symbol, None)
        if not filled:
            self.stats.entries_cancelled += 1

    def _fail_entry(self, symbol: str) -> None:
        """Arming exits failed on the first fill (position already flattened by _arm_exits): cancel
        any remaining resting rungs and abandon the entry."""
        eo = self._entry_orders.pop(symbol, None)
        self.pending.pop(symbol, None)
        for o in (eo or {}).get("orders", []):
            try:
                if o.get("id") is not None:
                    self.client.cancel(symbol, o["id"])
            except BinanceError:
                pass

    def _sync_stop(self, symbol: str, pos: Position) -> None:
        o = self._orders.get(symbol)
        if not o:
            return
        new_stop = self.filters.get(symbol, pos.entry_price).round_price(pos.stop)
        if new_stop == o["stop_px"]:
            return
        try:
            if o.get("stop_id") is not None:
                self.client.cancel(symbol, o["stop_id"])
            o["stop_id"] = self.client.stop_market(symbol, o["exit_side"], new_stop).get("orderId")
            o["stop_px"] = new_stop
            log.info("trailed stop %s -> %.6g", symbol, new_stop)
        except BinanceError:
            log.exception("failed to move stop for %s", symbol)

    def _exit_price_reason(self, symbol: str, pos: Position) -> tuple[float, str]:
        """Inspect the protective orders to see which one filled, for the exit label and fill price."""
        o = self._orders.get(symbol, {})
        exit_px, reason = self.last_price.get(symbol, pos.entry_price), "exit"
        for oid, label in ((o.get("tp_id"), "take_profit"), (o.get("stop_id"), "stop_loss")):
            if oid is None:
                continue
            try:
                st = self.client.order_status(symbol, oid)
            except BinanceError:
                continue
            if st.get("status") == "FILLED":
                reason = label
                exit_px = float(st.get("avgPrice") or st.get("stopPrice") or exit_px)
                break
        return exit_px, reason

    def _record_exchange_close(self, symbol: str, pos: Position, ts: int) -> None:
        exit_px, reason = self._exit_price_reason(symbol, pos)
        try:
            self.client.cancel_all(symbol)                        # clear the other protective order
        except BinanceError:
            pass
        self._orders.pop(symbol, None)
        self._finalize_close(symbol, pos, exit_px, ts, reason)

    def _close_now(self, symbol: str, pos: Position, ts: int, reason: str) -> Trade | None:
        o = self._orders.get(symbol, {})
        exit_side = o.get("exit_side") or self._exit_side(pos.side)
        try:
            self.client.cancel_all(symbol)
            resp = self.client.market(symbol, exit_side, pos.qty, reduce_only=True)
            exit_px = float(resp.get("avgPrice") or 0.0) or self.last_price.get(symbol, pos.entry_price)
        except BinanceError:
            log.exception("market close failed for %s — position may still be open, CHECK MANUALLY", symbol)
            return None
        self._orders.pop(symbol, None)
        return self._finalize_close(symbol, pos, exit_px, ts, reason)

    def _finalize_close(self, symbol: str, pos: Position, exit_px: float, ts: int, reason: str) -> Trade:
        fee_rate = self.cfg.maker_fee if reason == "take_profit" else self.cfg.taker_fee
        gross = pos.unrealized(exit_px)
        fee = pos.qty * exit_px * fee_rate
        self.positions.pop(symbol, None)
        self._sync_balance()
        trade = Trade(symbol, pos.side, pos.qty, pos.entry_price, exit_px, pos.entry_ts, ts, gross,
                      pos.entry_fee + fee, pos.funding, reason, pos.risk_usd, pos.reason, pos.stop,
                      pos.take_profit, pos.fills, pos.signal_ts, pos.rungs, pos.confidence)
        self.trades.append(trade)
        for cb in self.on_trade:
            cb(trade)
        return trade
