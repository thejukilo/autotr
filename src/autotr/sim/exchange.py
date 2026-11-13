"""Paper exchange simulating Binance USDT-M perpetuals with isolated margin.

Entries are *plans* made of one or more rungs: a market rung (price=None) or limit rungs (a
ladder). Filled rungs merge into a single position (average entry, combined margin); the stop and
take-profit always cover the whole filled quantity, and unfilled rungs are cancelled when the trade
exits, when they expire, or when price reaches the target before anything filled.

Two price-event models drive fills:
  * `on_bar(bar)`   — backtests/replays on 1m OHLC. Market rungs fill at the open; limit rungs when
                      price trades through them. Stop is checked before take-profit (conservative),
                      and a bar that filled a limit rung cannot also take profit.
  * `on_quote(...)` — live trading on best bid/ask updates.

Accounting: the wallet `balance` holds realised cash. Entry fees are charged per fill, the exit fee
at exit, funding as it occurs. Isolated margin is reserved per position; liquidation loses it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Callable

from autotr.config import ExchangeCfg
from autotr.models import Bar, Side, Trade


@dataclass(slots=True)
class Rung:
    price: float | None  # None = market order
    qty: float
    filled: bool = False


@dataclass(slots=True)
class PendingEntry:
    """An entry plan. Without explicit `rungs` it is a single market order of `qty`."""
    symbol: str
    side: Side
    qty: float
    stop: float
    take_profit: float
    risk_usd: float
    created_ts: int
    time_stop_ms: int
    reason: str = ""
    rungs: list[Rung] = field(default_factory=list)
    expiry_ts: int | None = None  # unfilled limit rungs are cancelled after this time
    confidence: float = 1.0

    def __post_init__(self) -> None:
        if not self.rungs:
            self.rungs = [Rung(None, self.qty)]
        self.qty = sum(r.qty for r in self.rungs)

    @property
    def open_rungs(self) -> list[Rung]:
        return [r for r in self.rungs if not r.filled]


@dataclass(slots=True)
class Position:
    symbol: str
    side: Side
    qty: float
    entry_price: float  # average of all fills
    entry_ts: int
    stop: float
    take_profit: float
    margin: float
    liq_price: float
    entry_fee: float
    risk_usd: float      # loss at the stop for the filled quantity, incl. costs
    time_stop_ts: int
    reason: str = ""
    funding: float = 0.0
    fills: int = 1
    signal_ts: int = 0
    rungs: tuple[float, ...] = ()
    confidence: float = 1.0

    @property
    def notional(self) -> float:
        return self.qty * self.entry_price

    def unrealized(self, price: float) -> float:
        return self.side.sign * (price - self.entry_price) * self.qty


@dataclass
class ExchangeStats:
    entries_filled: int = 0       # positions opened
    entries_cancelled: int = 0    # plans that never filled
    rungs_filled: int = 0
    liquidations: int = 0
    fees_paid: float = 0.0
    funding_paid: float = 0.0     # positive = received


def liquidation_price(side: Side, entry: float, leverage: float, mmr: float) -> float:
    """Isolated-margin liquidation price (one-way mode, single position).

    Liquidated when margin + unrealised PnL = maintenance margin (mmr × notional at that price).
    """
    if side is Side.LONG:
        return entry * (1 - 1 / leverage) / (1 - mmr)
    return entry * (1 + 1 / leverage) / (1 + mmr)


class PaperExchange:
    def __init__(self, cfg: ExchangeCfg, starting_balance: float):
        self.cfg = cfg
        self.balance = starting_balance
        self.positions: dict[str, Position] = {}
        self.pending: dict[str, PendingEntry] = {}
        self.trades: list[Trade] = []
        self.last_price: dict[str, float] = {}
        self.stats = ExchangeStats()
        self.on_trade: list[Callable[[Trade], None]] = []
        self.on_open: list[Callable[[Position], None]] = []

    # ------------------------------------------------------------ account state
    @property
    def slip(self) -> float:
        return self.cfg.slippage_bps / 10_000

    @property
    def used_margin(self) -> float:
        return sum(p.margin for p in self.positions.values())

    def unrealized_pnl(self) -> float:
        return sum(p.unrealized(self.last_price.get(s, p.entry_price)) for s, p in self.positions.items())

    def equity(self) -> float:
        """Wallet balance + unrealised PnL (what the account is worth if closed at last prices)."""
        return self.balance + self.unrealized_pnl()

    def available_balance(self) -> float:
        reserved = 0.0
        for s, pe in self.pending.items():
            for r in pe.open_rungs:
                reserved += r.qty * (r.price if r.price is not None else self.last_price.get(s, 0.0))
        return self.balance - self.used_margin - reserved / self.cfg.leverage

    def busy(self, symbol: str) -> bool:
        return symbol in self.positions or symbol in self.pending

    # ------------------------------------------------------------ orders
    def submit_entry(self, entry: PendingEntry) -> bool:
        if self.busy(entry.symbol):
            return False
        self.pending[entry.symbol] = entry
        return True

    def _cancel_plan(self, symbol: str) -> None:
        pe = self.pending.pop(symbol, None)
        if pe is not None and symbol not in self.positions and not any(r.filled for r in pe.rungs):
            self.stats.entries_cancelled += 1

    def _fill(self, pe: PendingEntry, rung: Rung, price: float, ts: int, fee_rate: float) -> bool:
        """Fill one rung at `price`. Returns False (and cancels the plan) if the fill is invalid."""
        s = pe.side.sign
        if s * (price - pe.stop) <= 0 or s * (pe.take_profit - price) <= 0:
            self._cancel_plan(pe.symbol)  # market already beyond stop/target: setup is void
            return False
        notional = rung.qty * price
        margin = notional / self.cfg.leverage
        fee = notional * fee_rate
        if margin + fee > self.balance - self.used_margin:
            self._cancel_plan(pe.symbol)
            return False
        cost = self.cfg.taker_fee + self.slip
        risk = rung.qty * (abs(price - pe.stop) + price * fee_rate + pe.stop * cost)
        rung.filled = True
        self.balance -= fee
        self.stats.fees_paid += fee
        self.stats.rungs_filled += 1
        pos = self.positions.get(pe.symbol)
        if pos is None:
            liq = liquidation_price(pe.side, price, self.cfg.leverage, self.cfg.maintenance_margin_rate)
            pos = Position(pe.symbol, pe.side, rung.qty, price, ts, pe.stop, pe.take_profit, margin, liq,
                           fee, risk, ts + pe.time_stop_ms, pe.reason, signal_ts=pe.created_ts,
                           rungs=tuple(r.price for r in pe.rungs if r.price is not None),
                           confidence=pe.confidence)
            self.positions[pe.symbol] = pos
            self.stats.entries_filled += 1
            for cb in self.on_open:
                cb(pos)
        else:
            q = pos.qty + rung.qty
            pos.entry_price = (pos.entry_price * pos.qty + price * rung.qty) / q
            pos.qty = q
            pos.margin += margin
            pos.entry_fee += fee
            pos.risk_usd += risk
            pos.fills += 1
            pos.liq_price = liquidation_price(pe.side, pos.entry_price, self.cfg.leverage,
                                              self.cfg.maintenance_margin_rate)
        if not pe.open_rungs:
            self.pending.pop(pe.symbol, None)
        return True

    def _close(self, pos: Position, price: float, ts: int, reason: str, fee_rate: float) -> Trade:
        self.pending.pop(pos.symbol, None)  # cancel any unfilled rungs
        gross = pos.unrealized(price)
        fee = pos.qty * price * fee_rate
        self.balance += gross - fee
        self.stats.fees_paid += fee
        del self.positions[pos.symbol]
        trade = Trade(pos.symbol, pos.side, pos.qty, pos.entry_price, price, pos.entry_ts, ts,
                      gross, pos.entry_fee + fee, pos.funding, reason, pos.risk_usd, pos.reason,
                      pos.stop, pos.take_profit, pos.fills, pos.signal_ts, pos.rungs, pos.confidence)
        self.trades.append(trade)
        for cb in self.on_trade:
            cb(trade)
        return trade

    def _liquidate(self, pos: Position, ts: int) -> Trade:
        self.stats.liquidations += 1
        self.pending.pop(pos.symbol, None)
        # Isolated margin is lost entirely; express it as gross loss at the liquidation price.
        self.balance -= pos.margin
        del self.positions[pos.symbol]
        trade = Trade(pos.symbol, pos.side, pos.qty, pos.entry_price, pos.liq_price, pos.entry_ts, ts,
                      -pos.margin, pos.entry_fee, pos.funding, "liquidation", pos.risk_usd, pos.reason,
                      pos.stop, pos.take_profit, pos.fills, pos.signal_ts, pos.rungs, pos.confidence)
        self.trades.append(trade)
        for cb in self.on_trade:
            cb(trade)
        return trade

    def close_market(self, symbol: str, price: float, ts: int, reason: str = "manual") -> Trade | None:
        pos = self.positions.get(symbol)
        if pos is None:
            return None
        fill = price * (1 - pos.side.sign * self.slip)
        return self._close(pos, fill, ts, reason, self.cfg.taker_fee)

    # ------------------------------------------------------------ OHLC model
    def on_bar(self, bar: Bar) -> None:
        sym = bar.symbol
        self.last_price[sym] = bar.close
        cfg, slip = self.cfg, self.slip
        pos_before = self.positions.get(sym)
        pe = self.pending.get(sym)
        limit_filled = False

        # 1. market rungs at the open
        if pe is not None:
            for r in pe.open_rungs:
                if r.price is None:
                    if not self._fill(pe, r, bar.open * (1 + pe.side.sign * slip), bar.open_time, cfg.taker_fee):
                        break
            pe = self.pending.get(sym)

        # 2. gaps at the open for a position that existed before this bar
        if pos_before is not None and sym in self.positions:
            pos = pos_before
            s = pos.side.sign
            if s * (bar.open - pos.liq_price) <= 0:
                self._liquidate(pos, bar.open_time)
                return
            if s * (bar.open - pos.stop) <= 0:
                self._close(pos, bar.open * (1 - s * slip), bar.open_time, "stop_loss (gap)", cfg.taker_fee)
                return
            if s * (bar.open - pos.take_profit) >= 0:
                self._close(pos, bar.open, bar.open_time, "take_profit (gap)", cfg.maker_fee)
                return

        # 3. limit rungs (nearest first: price travels through the ladder in order)
        if pe is not None:
            s = pe.side.sign
            for r in sorted(pe.open_rungs, key=lambda r: -s * (r.price or 0)):
                if r.price is None:
                    continue
                if s * (bar.open - r.price) <= 0:
                    fill = bar.open                  # gapped through the limit: better price
                elif (bar.low < r.price) if s > 0 else (bar.high > r.price):
                    fill = r.price
                else:
                    break                            # deeper rungs can't fill either
                if not self._fill(pe, r, fill, bar.open_time, cfg.maker_fee):
                    break
                limit_filled = True

        pos = self.positions.get(sym)
        pe = self.pending.get(sym)
        if pos is None:
            # Target reached before any rung filled, or the ladder expired: the setup is gone.
            if pe is not None:
                s = pe.side.sign
                if s * ((bar.high if s > 0 else bar.low) - pe.take_profit) >= 0 or \
                        (pe.expiry_ts is not None and bar.close_time >= pe.expiry_ts):
                    self._cancel_plan(sym)
            return

        # 4. stop first, then take-profit (not in a bar that just filled a limit rung)
        s = pos.side.sign
        if (bar.low if s > 0 else bar.high) * s <= pos.stop * s:
            self._close(pos, pos.stop * (1 - s * slip), bar.close_time, "stop_loss", cfg.taker_fee)
            return
        if not limit_filled and ((bar.high > pos.take_profit) if s > 0 else (bar.low < pos.take_profit)):
            self._close(pos, pos.take_profit, bar.close_time, "take_profit", cfg.maker_fee)
            return
        if pe is not None and pe.expiry_ts is not None and bar.close_time >= pe.expiry_ts:
            self.pending.pop(sym, None)              # keep the position, drop unfilled rungs
        if bar.close_time >= pos.time_stop_ts:
            self.close_market(sym, bar.close, bar.close_time, "time_stop")

    # ------------------------------------------------------------ tick model
    def on_quote(self, symbol: str, bid: float, ask: float, ts: int) -> None:
        self.last_price[symbol] = (bid + ask) / 2
        cfg, slip = self.cfg, self.slip
        pe = self.pending.get(symbol)
        limit_filled = False
        if pe is not None:
            s = pe.side.sign
            touch = ask if s > 0 else bid           # price we could buy (long) / sell (short) at
            for r in sorted(pe.open_rungs, key=lambda r: -s * (r.price if r.price is not None else touch)):
                if r.price is None:
                    ok = self._fill(pe, r, touch * (1 + s * slip), ts, cfg.taker_fee)
                elif s * (touch - r.price) <= 0:
                    ok = self._fill(pe, r, r.price, ts, cfg.maker_fee)
                    limit_filled = limit_filled or ok
                else:
                    break
                if not ok:
                    break
        pos = self.positions.get(symbol)
        pe = self.pending.get(symbol)
        if pos is None:
            if pe is not None:
                s = pe.side.sign
                exit_px = bid if s > 0 else ask
                if s * (exit_px - pe.take_profit) >= 0 or (pe.expiry_ts is not None and ts >= pe.expiry_ts):
                    self._cancel_plan(symbol)
            return
        s = pos.side.sign
        exit_px = bid if s > 0 else ask               # price we could close at
        if s * (exit_px - pos.liq_price) <= 0:
            self._liquidate(pos, ts)
        elif s * (exit_px - pos.stop) <= 0:
            self._close(pos, exit_px * (1 - s * slip), ts, "stop_loss", cfg.taker_fee)
        elif not limit_filled and s * (exit_px - pos.take_profit) >= 0:
            self._close(pos, pos.take_profit, ts, "take_profit", cfg.maker_fee)
        elif ts >= pos.time_stop_ts:
            self._close(pos, exit_px * (1 - s * slip), ts, "time_stop", cfg.taker_fee)
        elif pe is not None and pe.expiry_ts is not None and ts >= pe.expiry_ts:
            self.pending.pop(symbol, None)

    # ------------------------------------------------------------ funding
    def on_funding(self, symbol: str, rate: float, mark_price: float, ts: int) -> float:
        """Apply a funding event. Positive rate: longs pay shorts. Returns amount received."""
        pos = self.positions.get(symbol)
        if pos is None:
            return 0.0
        amount = -pos.side.sign * pos.qty * mark_price * rate
        self.balance += amount
        pos.funding += amount
        self.stats.funding_paid += amount
        return amount

    # ------------------------------------------------------------ persistence
    def to_state(self) -> dict:
        def enc(obj) -> dict:
            d = asdict(obj)
            d["side"] = obj.side.value
            return d
        return {
            "balance": self.balance,
            "positions": [enc(p) for p in self.positions.values()],
            "pending": [enc(p) for p in self.pending.values()],
            "stats": asdict(self.stats),
            "last_price": self.last_price,
        }

    def load_state(self, state: dict) -> None:
        self.balance = state["balance"]
        self.positions = {d["symbol"]: Position(**{**d, "side": Side(d["side"]), "rungs": tuple(d.get("rungs", ()))})
                          for d in state["positions"]}
        self.pending = {}
        for d in state["pending"]:
            rungs = [Rung(**r) for r in d.get("rungs", [])]
            self.pending[d["symbol"]] = PendingEntry(**{**d, "side": Side(d["side"]), "rungs": rungs})
        self.stats = ExchangeStats(**state.get("stats", {}))
        self.last_price = dict(state.get("last_price", {}))
