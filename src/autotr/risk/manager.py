"""Position sizing and account-level guards.

Size = (risk_per_trade × equity) / (stop distance + expected round-trip costs per unit),
then capped so one position never uses more than `max_margin_per_position` of equity as margin
at the configured leverage, nor more than the free balance. Because of that cap, tight scalping
stops often end up risking *less* than the nominal 2% — the actual risk is recorded per trade.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from autotr.config import ExchangeCfg, RiskCfg, StrategyCfg
from autotr.models import DAY_MS, MINUTE_MS, Side, Signal, Trade
from autotr.sim.exchange import PaperExchange, PendingEntry, liquidation_price
from autotr.sim.filters import FilterBook


@dataclass
class RiskState:
    day: int = -1
    day_start_equity: float = 0.0
    trades_today: int = 0
    halted: bool = False
    last_exit_ts: dict[str, int] = field(default_factory=dict)


class RiskManager:
    def __init__(self, cfg: RiskCfg, ex_cfg: ExchangeCfg, strat_cfg: StrategyCfg, filters: FilterBook):
        self.cfg = cfg
        self.ex_cfg = ex_cfg
        self.strat_cfg = strat_cfg
        self.filters = filters
        self.state = RiskState()
        self.rejections: Counter[str] = Counter()

    # ------------------------------------------------------------ bookkeeping
    def on_trade(self, trade: Trade) -> None:
        self.state.last_exit_ts[trade.symbol] = trade.exit_ts

    def update_clock(self, ts: int, equity: float) -> None:
        """Call regularly (every bar). Handles UTC-day rollover and the daily loss limit."""
        st = self.state
        day = ts // DAY_MS
        if day != st.day:
            st.day = day
            st.day_start_equity = equity
            st.trades_today = 0
            st.halted = False
        if not st.halted and st.day_start_equity > 0:
            if equity <= st.day_start_equity * (1 - self.cfg.daily_loss_limit):
                st.halted = True

    def _reject(self, reason: str) -> None:
        self.rejections[reason] += 1

    # ------------------------------------------------------------ decision
    def evaluate(self, sig: Signal, ex: PaperExchange) -> PendingEntry | None:
        st, cfg, xc = self.state, self.cfg, self.ex_cfg
        if st.halted:
            return self._reject("daily_loss_limit")
        if st.trades_today >= cfg.max_trades_per_day:
            return self._reject("max_trades_per_day")
        if ex.busy(sig.symbol):
            return self._reject("already_in_symbol")
        if len(ex.positions) + len(ex.pending) >= cfg.max_positions:
            return self._reject("max_positions")
        last_exit = st.last_exit_ts.get(sig.symbol)
        if last_exit is not None and sig.ts - last_exit < self.strat_cfg.cooldown_minutes * MINUTE_MS:
            return self._reject("cooldown")

        f = self.filters.get(sig.symbol, sig.ref_price)
        entry = sig.ref_price
        stop = f.round_price(sig.stop)
        tp = f.round_price(sig.take_profit)
        s = sig.side.sign
        stop_dist = s * (entry - stop)
        if stop_dist <= 0 or s * (tp - entry) <= 0:
            return self._reject("invalid_levels")

        cost_rate = xc.taker_fee + xc.slippage_bps / 10_000
        per_unit_loss = stop_dist + entry * cost_rate + stop * cost_rate
        equity = ex.equity()
        risk_budget = equity * cfg.risk_per_trade
        qty = risk_budget / per_unit_loss

        max_notional = min(
            equity * cfg.max_margin_per_position * xc.leverage,
            max(ex.available_balance(), 0.0) * xc.leverage * 0.98,  # leave room for fees
        )
        qty = min(qty, max_notional / entry)
        qty = f.round_qty(qty)
        if qty < f.min_qty or qty * entry < f.min_notional:
            return self._reject("below_min_notional")

        liq = liquidation_price(sig.side, entry, xc.leverage, xc.maintenance_margin_rate)
        if s * (stop - liq) <= 0:
            return self._reject("stop_beyond_liquidation")

        st.trades_today += 1
        return PendingEntry(
            symbol=sig.symbol, side=sig.side, qty=qty, stop=stop, take_profit=tp,
            risk_usd=qty * per_unit_loss, created_ts=sig.ts,
            time_stop_ms=self.strat_cfg.time_stop_minutes * MINUTE_MS, reason=sig.reason,
        )

    # ------------------------------------------------------------ persistence
    def to_state(self) -> dict:
        s = self.state
        return {"day": s.day, "day_start_equity": s.day_start_equity, "trades_today": s.trades_today,
                "halted": s.halted, "last_exit_ts": s.last_exit_ts}

    def load_state(self, d: dict) -> None:
        self.state = RiskState(d["day"], d["day_start_equity"], d["trades_today"], d["halted"],
                               dict(d.get("last_exit_ts", {})))
