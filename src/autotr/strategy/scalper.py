"""Mean-reversion scalper: RSI extreme turning back at a support/resistance zone or trendline.

LONG  when 1m RSI crosses back above `rsi_oversold` and in the last few 1m bars price touched
      (within `proximity_atr` × ATR5m) a support zone below price or an ascending trendline,
      and neither the current price nor the last 5m close broke through that level.
SHORT is the mirror image at resistance / descending trendlines with RSI crossing back below
      `rsi_overbought`.

Stop: just beyond the level (`stop_buffer_atr` × ATR1m), clamped to [min_stop_pct, max_stop_pct].
Target: `take_profit_r` × risk, or the next opposing zone if closer. Trades whose reward/risk
after fees is below `min_reward_risk_after_fees` are skipped.
"""

from __future__ import annotations

from dataclasses import dataclass

from autotr.config import ExchangeCfg, StrategyCfg
from autotr.market.features import LevelContext, SymbolState
from autotr.models import Side, Signal


@dataclass(slots=True)
class _Level:
    price: float
    label: str


class ScalperStrategy:
    def __init__(self, cfg: StrategyCfg, fees: ExchangeCfg):
        self.cfg = cfg
        self.fees = fees

    # ----------------------------------------------------------------- helpers
    def _support_levels(self, st: SymbolState, ctx: LevelContext, price: float, now_ts: int) -> list[_Level]:
        c = self.cfg
        prox = c.proximity_atr * ctx.atr5
        recent_low = min(b.low for b in list(st.bars1)[-c.touch_lookback_bars:])
        out: list[_Level] = []
        for z in ctx.zones:
            if z.low > price:
                break  # zones are sorted ascending; the rest are above price
            if recent_low <= z.high + prox and ctx.last5_close >= z.low:
                out.append(_Level(z.low, f"support zone x{z.touches}"))
        line = ctx.support_line
        if line is not None:
            v = line.value_at(now_ts)
            v5 = line.value_at(ctx.last5_open_time)
            tol = c.trendline_break_tolerance_atr * ctx.atr5
            if v < price and recent_low <= v + prox and ctx.last5_close >= v5 - tol:
                out.append(_Level(v, f"rising trendline x{line.touches}"))
        return out

    def _resistance_levels(self, st: SymbolState, ctx: LevelContext, price: float, now_ts: int) -> list[_Level]:
        c = self.cfg
        prox = c.proximity_atr * ctx.atr5
        recent_high = max(b.high for b in list(st.bars1)[-c.touch_lookback_bars:])
        out: list[_Level] = []
        for z in reversed(ctx.zones):
            if z.high < price:
                break
            if recent_high >= z.low - prox and ctx.last5_close <= z.high:
                out.append(_Level(z.high, f"resistance zone x{z.touches}"))
        line = ctx.resistance_line
        if line is not None:
            v = line.value_at(now_ts)
            v5 = line.value_at(ctx.last5_open_time)
            tol = c.trendline_break_tolerance_atr * ctx.atr5
            if v > price and recent_high >= v - prox and ctx.last5_close <= v5 + tol:
                out.append(_Level(v, f"falling trendline x{line.touches}"))
        return out

    def reward_risk_after_fees(self, side: Side, entry: float, stop: float, tp: float) -> float:
        f = self.fees
        slip = f.slippage_bps / 10_000
        s = side.sign
        reward = s * (tp - entry) - entry * (f.taker_fee + slip) - tp * f.maker_fee
        risk = s * (entry - stop) + entry * (f.taker_fee + slip) + stop * (f.taker_fee + slip)
        return reward / risk if risk > 0 else 0.0

    # ----------------------------------------------------------------- main
    def on_bar(self, st: SymbolState) -> Signal | None:
        c = self.cfg
        r0, r1 = st.rsi_prev, st.rsi_now
        if r0 is None or r1 is None or st.last_bar is None:
            return None
        go_long = r0 < c.rsi_oversold <= r1
        go_short = r0 > c.rsi_overbought >= r1
        if not (go_long or go_short):
            return None  # cheap exit: most bars stop here

        ctx = st.level_context()
        if ctx is None or st.atr1.value is None:
            return None
        bar = st.last_bar
        price, now_ts, atr1 = bar.close, bar.close_time, float(st.atr1.value)
        side = Side.LONG if go_long else Side.SHORT

        if side is Side.LONG:
            levels = self._support_levels(st, ctx, price, now_ts)
            if not levels:
                return None
            level = max(levels, key=lambda lv: lv.price)  # closest support below price
            stop = level.price - c.stop_buffer_atr * atr1
            stop = min(stop, price * (1 - c.min_stop_pct))
            if price - stop > price * c.max_stop_pct:
                return None
            risk = price - stop
            tp = price + c.take_profit_r * risk
            above = [z for z in ctx.zones if z.low > price]
            if above:
                tp = min(tp, above[0].low)
        else:
            levels = self._resistance_levels(st, ctx, price, now_ts)
            if not levels:
                return None
            level = min(levels, key=lambda lv: lv.price)  # closest resistance above price
            stop = level.price + c.stop_buffer_atr * atr1
            stop = max(stop, price * (1 + c.min_stop_pct))
            if stop - price > price * c.max_stop_pct:
                return None
            risk = stop - price
            tp = price - c.take_profit_r * risk
            below = [z for z in ctx.zones if z.high < price]
            if below:
                tp = max(tp, below[-1].high)

        rr = self.reward_risk_after_fees(side, price, stop, tp)
        if rr < c.min_reward_risk_after_fees:
            return None
        confluence = " + ".join(lv.label for lv in levels)
        reason = f"RSI {r0:.0f}->{r1:.0f} at {confluence} (R:R {rr:.2f} after fees)"
        return Signal(st.symbol, side, now_ts, price, stop, tp, reason)
