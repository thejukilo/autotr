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
from autotr.models import DAY_MS, MINUTE_MS, Signal, Trade
from autotr.sim.exchange import PaperExchange, PendingEntry, Rung, liquidation_price
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

    def _risk_fraction(self, sig: Signal) -> float:
        """Fraction of equity to risk on this trade. With confidence_sizing on, it scales linearly
        with the signal's conviction (0..1) between conf_risk_min and conf_risk_max; otherwise flat."""
        cfg = self.cfg
        if not (cfg.confidence_sizing and cfg.conf_risk_max > 0):
            return cfg.risk_per_trade
        lo = cfg.conf_risk_min if cfg.conf_risk_min > 0 else cfg.risk_per_trade
        conf = min(max(sig.confidence, 0.0), 1.0)
        return lo + (cfg.conf_risk_max - lo) * conf

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
        stop = f.round_price(sig.stop)
        tp = f.round_price(sig.take_profit)
        s = sig.side.sign
        slip = xc.slippage_bps / 10_000
        exit_cost = xc.taker_fee + slip
        ladder = bool(sig.ladder) and self.strat_cfg.use_ladder
        if ladder:
            levels = [(f.round_price(p), w) for p, w in sig.ladder]  # type: ignore[union-attr]
            entry_cost = xc.maker_fee          # resting limit orders
        else:
            levels = [(sig.ref_price, 1.0)]    # single market order
            entry_cost = xc.taker_fee + slip
        if any(s * (p - stop) <= 0 or s * (tp - p) <= 0 for p, _ in levels):
            return self._reject("invalid_levels")

        def unit_loss(p: float) -> float:
            return s * (p - stop) + p * entry_cost + stop * exit_cost

        equity = ex.equity()
        risk_budget = equity * self._risk_fraction(sig)
        avg_price = sum(p * w for p, w in levels)
        qty = risk_budget / sum(w * unit_loss(p) for p, w in levels)  # full fill risks exactly the budget
        max_notional = min(
            equity * cfg.max_margin_per_position * xc.leverage,
            max(ex.available_balance(), 0.0) * xc.leverage * 0.98,  # leave room for fees
        )
        qty = min(qty, max_notional / avg_price)

        # Split into rungs; a rung below the exchange minimum is merged into the next (deeper) one.
        rungs: list[Rung] = []
        carry = 0.0
        for p, w in levels:
            raw = qty * w + carry
            q = f.round_qty(raw)
            if q < f.min_qty or q * p < f.min_notional:
                carry = raw
                continue
            rungs.append(Rung(p if ladder else None, q))
            carry = 0.0
        if not rungs:
            return self._reject("below_min_notional")

        worst_entry = max(r_p for r_p, _ in levels) if s > 0 else min(r_p for r_p, _ in levels)
        liq = liquidation_price(sig.side, worst_entry, xc.leverage, xc.maintenance_margin_rate)
        if s * (stop - liq) <= 0:
            return self._reject("stop_beyond_liquidation")

        st.trades_today += 1
        prices = [r.price if r.price is not None else sig.ref_price for r in rungs]
        expiry = (sig.ts + self.strat_cfg.ladder_expiry_bars * self.strat_cfg.entry_tf_minutes * MINUTE_MS
                  if ladder else None)
        return PendingEntry(
            symbol=sig.symbol, side=sig.side, qty=sum(r.qty for r in rungs), stop=stop, take_profit=tp,
            risk_usd=sum(r.qty * unit_loss(p) for r, p in zip(rungs, prices)), created_ts=sig.ts,
            time_stop_ms=self.strat_cfg.time_stop_minutes * MINUTE_MS, reason=sig.reason,
            rungs=rungs, expiry_ts=expiry, confidence=sig.confidence,
        )

    # ------------------------------------------------------------ persistence
    def to_state(self) -> dict:
        s = self.state
        return {"day": s.day, "day_start_equity": s.day_start_equity, "trades_today": s.trades_today,
                "halted": s.halted, "last_exit_ts": s.last_exit_ts}

    def load_state(self, d: dict) -> None:
        self.state = RiskState(d["day"], d["day_start_equity"], d["trades_today"], d["halted"],
                               dict(d.get("last_exit_ts", {})))
