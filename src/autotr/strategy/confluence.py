"""Confluence strategy — the user's manual method, made mechanical.

On every closed entry-timeframe bar (5m) of a coin picked by the scanner (high volume, big 24h move):

1. **Gate**: RSI must have been oversold (long) / overbought (short) within the last
   `rsi_lookback` entry bars.
2. **Score** the structure on the structure timeframe (1h) at the current price:
     RSI present (+w_rsi), RSI extreme (+w_rsi_extreme), RSI turning back (+w_rsi_turn),
     at a support/resistance zone (+w_zone, +w_zone_strong if ≥ `strong_zone_touches`),
     at a channel boundary (+w_channel), at a trendline (+w_trendline),
     completed Elliott 5-wave impulse in the opposite direction, i.e. a wave-5 fade (+w_elliott).
   Trade only if the score ≥ `min_score`.
3. **Ladder**: three limit orders from just below the current price down to the level (longs;
   mirrored for shorts), weighted `ladder_weights` (biggest closest to the level).
4. **Stop** beyond the structure the setup relies on (the zone, channel edge, trendline or the
   wave-5 extreme), falling back to the nearest strong zone / swing pivot, plus a buffer.
5. **Target** at the first opposing S/R zone (or channel boundary / wave-4 area) that gives at
   least `min_reward_risk_after_fees`; otherwise no trade.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from autotr.config import ExchangeCfg, StrategyCfg
from autotr.market.features import LevelContext, SymbolState
from autotr.models import Side, Signal


@dataclass
class Setup:
    side: Side
    score: float = 0.0
    parts: list[str] = field(default_factory=list)
    anchor: float | None = None          # the level price is bouncing from (closest to price)
    stop_refs: list[float] = field(default_factory=list)    # structures the stop must sit beyond
    targets: list[tuple[float, str]] = field(default_factory=list)

    def add(self, points: float, label: str) -> None:
        if points:
            self.score += points
            self.parts.append(f"{label} +{points:g}")


class ConfluenceStrategy:
    def __init__(self, cfg: StrategyCfg, fees: ExchangeCfg):
        self.cfg = cfg
        self.fees = fees

    # ------------------------------------------------------------------ scoring
    def _score(self, side: Side, st: SymbolState, ctx: LevelContext, price: float, now: int) -> Setup:
        c = self.cfg
        s = side.sign
        su = Setup(side)
        recent = list(st.rsi_hist)[-max(c.rsi_lookback, 1):]
        extreme_rsi = min(recent) if s > 0 else max(recent)
        su.add(c.w_rsi, f"RSI {extreme_rsi:.0f}")
        if (s > 0 and extreme_rsi <= c.rsi_extreme_low) or (s < 0 and extreme_rsi >= c.rsi_extreme_high):
            su.add(c.w_rsi_extreme, "RSI extreme")
        if st.rsi_prev is not None and st.rsi_now is not None and s * (st.rsi_now - st.rsi_prev) > 0:
            su.add(c.w_rsi_turn, "RSI turning")

        prox = c.proximity_atr * ctx.atr
        tol = c.trendline_break_tolerance_atr * ctx.atr
        bars = list(st.bars_e)[-c.touch_lookback_bars:]
        touch = min(b.low for b in bars) if s > 0 else max(b.high for b in bars)

        def at_level(v: float) -> bool:
            # price reached the level recently and is not through it
            return s * (touch - v) <= prox and s * (price - v) >= -tol and s * (ctx.last_close - v) >= -tol

        def anchor(v: float) -> None:
            if s * (price - v) >= 0 and (su.anchor is None or s * (v - su.anchor) > 0):
                su.anchor = v

        if c.use_zones:
            near = None
            for z in ctx.zones:
                edge = z.high if s > 0 else z.low      # the side of the zone price meets first
                far = z.low if s > 0 else z.high
                if at_level(edge) and s * (price - far) >= -tol:
                    if near is None or s * (edge - (near.high if s > 0 else near.low)) > 0:
                        near = z
            if near is not None:
                strong = near.touches >= c.strong_zone_touches
                su.add(c.w_zone + (c.w_zone_strong if strong else 0.0),
                       f"{'support' if s > 0 else 'resistance'} zone x{near.touches}")
                anchor(near.high if s > 0 else near.low)
                su.stop_refs.append(near.low if s > 0 else near.high)
        if c.use_channels and ctx.channel is not None:
            ch = ctx.channel
            edge = ch.lower(now) if s > 0 else ch.upper(now)
            if at_level(edge):
                su.add(c.w_channel, "channel " + ("floor" if s > 0 else "ceiling"))
                anchor(edge)
                su.stop_refs.append(edge)
                opp = ch.upper(now) if s > 0 else ch.lower(now)
                su.targets.append((opp, "channel " + ("ceiling" if s > 0 else "floor")))
        if c.use_trendlines:
            line = ctx.support_line if s > 0 else ctx.resistance_line
            if line is not None:
                v = line.value_at(now)
                if at_level(v):
                    su.add(c.w_trendline, ("rising" if s > 0 else "falling") + f" trendline x{line.touches}")
                    anchor(v)
                    su.stop_refs.append(v)
        if c.use_elliott and ctx.impulse is not None and ctx.impulse.fade_side == side.value:
            imp = ctx.impulse
            if s * (price - imp.end.price) > 0:          # wave-5 extreme still holds
                su.add(c.w_elliott, f"Elliott wave-5 fade (q {imp.quality:.2f})")
                anchor(imp.end.price)
                su.stop_refs.append(imp.end.price)
                su.targets.append((imp.wave4.price, "wave-4 area"))
        return su

    # ------------------------------------------------------------------ geometry
    def reward_risk(self, side: Side, entry: float, stop: float, tp: float, ladder: bool) -> float:
        f = self.fees
        slip = f.slippage_bps / 10_000
        entry_cost = f.maker_fee if ladder else f.taker_fee + slip
        s = side.sign
        reward = s * (tp - entry) - entry * entry_cost - tp * f.maker_fee
        risk = s * (entry - stop) + entry * entry_cost + stop * (f.taker_fee + slip)
        return reward / risk if risk > 0 else 0.0

    def _plan(self, su: Setup, st: SymbolState, ctx: LevelContext, price: float, now: int) -> Signal | None:
        c = self.cfg
        s = su.side.sign
        atr_e = float(st.atr_e.value)  # type: ignore[arg-type]
        use_ladder = c.use_ladder

        # Ladder: from just inside the current price towards the level
        top = price - s * 0.1 * atr_e
        level = su.anchor if su.anchor is not None else price - s * 0.5 * atr_e
        bottom = level if s * (top - level) >= 0.5 * atr_e else top - s * 0.5 * atr_e
        weights = list(c.ladder_weights) if use_ladder else [1.0]
        n = len(weights)
        rungs = [top - (top - bottom) * (i / (n - 1) if n > 1 else 0.0) for i in range(n)]
        entry = sum(p * w for p, w in zip(rungs, weights)) / sum(weights) if use_ladder else price
        deepest = rungs[-1] if use_ladder else price

        # Stop: beyond the structure(s) the setup relies on (zone / channel / trendline / wave-5
        # extreme). Without one behind the deepest rung, fall back to the nearest strong zone, then
        # the nearest swing pivot.
        refs = [r for r in su.stop_refs if s * (deepest - r) >= 0]
        if not refs:
            strong = [z for z in ctx.zones if z.touches >= c.strong_zone_touches
                      and s * (deepest - (z.low if s > 0 else z.high)) > 0]
            if strong:
                z = max(strong, key=lambda z: s * (z.low if s > 0 else z.high))
                refs.append(z.low if s > 0 else z.high)
        if not refs:
            piv = [p.price for p in ctx.pivots
                   if p.kind == ("low" if s > 0 else "high") and s * (deepest - p.price) > 0]
            if not piv:
                return None
            refs.append(max(piv) if s > 0 else min(piv))
        base = min(refs) if s > 0 else max(refs)            # the farthest structure
        stop = base - s * c.stop_buffer_atr * atr_e
        dist = s * (entry - stop) / entry
        if dist < c.min_stop_pct:
            stop = entry * (1 - s * c.min_stop_pct)
        if s * (entry - stop) / entry > c.max_stop_pct or s * (deepest - stop) <= 0:
            return None

        # Target: first opposing level that pays enough after fees
        cands = list(su.targets)
        for z in ctx.zones:
            edge = z.low if s > 0 else z.high
            if s * (edge - top) > 0:
                cands.append((edge, f"{'resistance' if s > 0 else 'support'} zone x{z.touches}"))
        cands = sorted({(round(p, 12), lbl) for p, lbl in cands if s * (p - top) > 0}, key=lambda x: s * x[0])
        for tp_level, label in cands:
            tp = tp_level - s * 0.1 * atr_e                  # just in front of the level
            if s * (tp - top) <= 0:
                continue
            rr = self.reward_risk(su.side, entry, stop, tp, use_ladder)
            if rr >= c.min_reward_risk_after_fees:
                reason = (f"score {su.score:g}: " + ", ".join(su.parts)
                          + f" | target {label} | R:R {rr:.2f} after fees")
                ladder = list(zip(rungs, [w / sum(weights) for w in weights])) if use_ladder else None
                return Signal(st.symbol, su.side, now, price, stop, tp, reason, ladder)
        return None

    # ------------------------------------------------------------------ main
    def on_bar(self, st: SymbolState) -> Signal | None:
        c = self.cfg
        if not st.ready or st.last_bar is None or not st.rsi_hist:
            return None
        recent = list(st.rsi_hist)[-max(c.rsi_lookback, 1):]
        sides = []
        if min(recent) <= c.rsi_oversold:
            sides.append(Side.LONG)
        if max(recent) >= c.rsi_overbought:
            sides.append(Side.SHORT)
        if not sides:
            return None  # cheap exit for most bars
        ctx = st.level_context()
        if ctx is None or st.atr_e.value is None:
            return None
        bar = st.last_bar
        price, now = bar.close, bar.close_time
        best: Signal | None = None
        best_score = -1.0
        for side in sides:
            su = self._score(side, st, ctx, price, now)
            if su.score < c.min_score or su.score <= best_score:
                continue
            sig = self._plan(su, st, ctx, price, now)
            if sig is not None:
                best, best_score = sig, su.score
        return best
