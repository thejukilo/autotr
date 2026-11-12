"""Paper exchange simulating Binance USDT-M perpetuals with isolated margin.

Two price-event models drive fills:
  * `on_bar(bar)`     — backtests/replays on 1m OHLC. Pending entries fill at the bar open; then
                        stop / take-profit are checked against the bar range, **stop first** when
                        both are inside the same bar (conservative).
  * `on_quote(...)`   — live trading on best bid/ask updates.

Accounting: the wallet `balance` holds realised cash. Opening fee is charged at entry, closing fee
at exit, funding as it occurs. Isolated margin is reserved per position; liquidation loses it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Callable

from autotr.config import ExchangeCfg
from autotr.models import Bar, Side, Trade


@dataclass(slots=True)
class PendingEntry:
    symbol: str
    side: Side
    qty: float
    stop: float
    take_profit: float
    risk_usd: float
    created_ts: int
    time_stop_ms: int
    reason: str = ""


@dataclass(slots=True)
class Position:
    symbol: str
    side: Side
    qty: float
    entry_price: float
    entry_ts: int
    stop: float
    take_profit: float
    margin: float
    liq_price: float
    entry_fee: float
    risk_usd: float
    time_stop_ts: int
    reason: str = ""
    funding: float = 0.0

    @property
    def notional(self) -> float:
        return self.qty * self.entry_price

    def unrealized(self, price: float) -> float:
        return self.side.sign * (price - self.entry_price) * self.qty


@dataclass
class ExchangeStats:
    entries_filled: int = 0
    entries_cancelled: int = 0
    liquidations: int = 0
    fees_paid: float = 0.0
    funding_paid: float = 0.0  # positive = received


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
        reserved = sum(pe.qty * self.last_price.get(s, 0.0) / self.cfg.leverage for s, pe in self.pending.items())
        return self.balance - self.used_margin - reserved

    def busy(self, symbol: str) -> bool:
        return symbol in self.positions or symbol in self.pending

    # ------------------------------------------------------------ orders
    def submit_entry(self, entry: PendingEntry) -> bool:
        if self.busy(entry.symbol):
            return False
        self.pending[entry.symbol] = entry
        return True

    def _open(self, pe: PendingEntry, fill_price: float, ts: int) -> Position | None:
        s = pe.side.sign
        # Market moved past the stop or target before we could fill: skip the trade.
        if s * (fill_price - pe.stop) <= 0 or s * (pe.take_profit - fill_price) <= 0:
            self.stats.entries_cancelled += 1
            return None
        notional = pe.qty * fill_price
        margin = notional / self.cfg.leverage
        fee = notional * self.cfg.taker_fee
        if margin + fee > self.balance - self.used_margin:
            self.stats.entries_cancelled += 1
            return None
        liq = liquidation_price(pe.side, fill_price, self.cfg.leverage, self.cfg.maintenance_margin_rate)
        pos = Position(pe.symbol, pe.side, pe.qty, fill_price, ts, pe.stop, pe.take_profit, margin,
                       liq, fee, pe.risk_usd, ts + pe.time_stop_ms, pe.reason)
        self.balance -= fee
        self.stats.fees_paid += fee
        self.stats.entries_filled += 1
        self.positions[pe.symbol] = pos
        for cb in self.on_open:
            cb(pos)
        return pos

    def _close(self, pos: Position, price: float, ts: int, reason: str, fee_rate: float) -> Trade:
        gross = pos.unrealized(price)
        fee = pos.qty * price * fee_rate
        self.balance += gross - fee
        self.stats.fees_paid += fee
        del self.positions[pos.symbol]
        trade = Trade(pos.symbol, pos.side, pos.qty, pos.entry_price, price, pos.entry_ts, ts,
                      gross, pos.entry_fee + fee, pos.funding, reason, pos.risk_usd, pos.reason)
        self.trades.append(trade)
        for cb in self.on_trade:
            cb(trade)
        return trade

    def _liquidate(self, pos: Position, ts: int) -> Trade:
        self.stats.liquidations += 1
        # Isolated margin is lost entirely; express it as gross loss at the liquidation price.
        self.balance -= pos.margin
        del self.positions[pos.symbol]
        trade = Trade(pos.symbol, pos.side, pos.qty, pos.entry_price, pos.liq_price, pos.entry_ts, ts,
                      -pos.margin, pos.entry_fee, pos.funding, "liquidation", pos.risk_usd, pos.reason)
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
        just_opened = False
        pe = self.pending.pop(sym, None)
        if pe is not None:
            fill = bar.open * (1 + pe.side.sign * self.slip)
            just_opened = self._open(pe, fill, bar.open_time) is not None

        pos = self.positions.get(sym)
        if pos is None:
            return
        cfg, slip = self.cfg, self.slip
        if pos.side is Side.LONG:
            if not just_opened and bar.open <= pos.liq_price:
                self._liquidate(pos, bar.open_time)
                return
            if not just_opened and bar.open <= pos.stop:
                self._close(pos, bar.open * (1 - slip), bar.open_time, "stop_loss (gap)", cfg.taker_fee)
                return
            if not just_opened and bar.open >= pos.take_profit:
                self._close(pos, bar.open, bar.open_time, "take_profit (gap)", cfg.maker_fee)
                return
            if bar.low <= pos.stop:
                self._close(pos, pos.stop * (1 - slip), bar.close_time, "stop_loss", cfg.taker_fee)
                return
            if bar.high > pos.take_profit:
                self._close(pos, pos.take_profit, bar.close_time, "take_profit", cfg.maker_fee)
                return
        else:
            if not just_opened and bar.open >= pos.liq_price:
                self._liquidate(pos, bar.open_time)
                return
            if not just_opened and bar.open >= pos.stop:
                self._close(pos, bar.open * (1 + slip), bar.open_time, "stop_loss (gap)", cfg.taker_fee)
                return
            if not just_opened and bar.open <= pos.take_profit:
                self._close(pos, bar.open, bar.open_time, "take_profit (gap)", cfg.maker_fee)
                return
            if bar.high >= pos.stop:
                self._close(pos, pos.stop * (1 + slip), bar.close_time, "stop_loss", cfg.taker_fee)
                return
            if bar.low < pos.take_profit:
                self._close(pos, pos.take_profit, bar.close_time, "take_profit", cfg.maker_fee)
                return
        if bar.close_time >= pos.time_stop_ts:
            self.close_market(sym, bar.close, bar.close_time, "time_stop")

    # ------------------------------------------------------------ tick model
    def on_quote(self, symbol: str, bid: float, ask: float, ts: int) -> None:
        self.last_price[symbol] = (bid + ask) / 2
        cfg, slip = self.cfg, self.slip
        pe = self.pending.pop(symbol, None)
        if pe is not None:
            fill = (ask if pe.side is Side.LONG else bid) * (1 + pe.side.sign * slip)
            self._open(pe, fill, ts)
        pos = self.positions.get(symbol)
        if pos is None:
            return
        if pos.side is Side.LONG:
            if bid <= pos.liq_price:
                self._liquidate(pos, ts)
            elif bid <= pos.stop:
                self._close(pos, bid * (1 - slip), ts, "stop_loss", cfg.taker_fee)
            elif bid >= pos.take_profit:
                self._close(pos, pos.take_profit, ts, "take_profit", cfg.maker_fee)
            elif ts >= pos.time_stop_ts:
                self._close(pos, bid * (1 - slip), ts, "time_stop", cfg.taker_fee)
        else:
            if ask >= pos.liq_price:
                self._liquidate(pos, ts)
            elif ask >= pos.stop:
                self._close(pos, ask * (1 + slip), ts, "stop_loss", cfg.taker_fee)
            elif ask <= pos.take_profit:
                self._close(pos, pos.take_profit, ts, "take_profit", cfg.maker_fee)
            elif ts >= pos.time_stop_ts:
                self._close(pos, ask * (1 + slip), ts, "time_stop", cfg.taker_fee)

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
        self.positions = {d["symbol"]: Position(**{**d, "side": Side(d["side"])}) for d in state["positions"]}
        self.pending = {d["symbol"]: PendingEntry(**{**d, "side": Side(d["side"])}) for d in state["pending"]}
        self.stats = ExchangeStats(**state.get("stats", {}))
        self.last_price = dict(state.get("last_price", {}))
