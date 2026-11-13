"""Momentum ignition on a fresh mover (5m): buy the first shallow dip, trailing exit, no target.

Long trigger: `ret_n`-bar (1h) return >= min_ret, last-hour volume >= vol_mult x the mean hourly
volume of the rest of the 24h window, the close at a new 24h high, change_24h >= min_change.
Mirror (new 24h low, falling) for shorts. Entry: limits at close - r1 x ATR5 and close - r2 x ATR5
(`market: true` = market at the signal). Stop = rung2 - stop_atr x ATR5, widened to at least
`min_stop` from the average entry. manage(): chandelier trail x ATR5 from the best close once the
trade is +trail_after_r R.
Hypothesis: attention shocks at the start of a move continue intraday; catching it in the first hour
(before it is a stale top mover) with a trailing exit keeps the profit tail.
Round 2 params: `longs` / `shorts` (side filter), `skip_hours` (UTC hours of the signal candle close).
Round 3 param: `max_change` (0 = off): skip signals with |change_24h| >= max_change (fresh movers only).
Ladder params (market=false): `rungs` (number of limit rungs, default 2) and `weights` (per-rung size,
deepest last; default equal) spread the entry evenly from close-r1xATR5 to close-r2xATR5.
"""

from __future__ import annotations

from autotr.models import Side
from autotr.strategy.research._common import efficiency_ratio, signal
from autotr.strategy.research._scalp import make_book, atr_bars, best_close, far_target, hour_ok, side_ok

DEFAULTS = dict(ret_n=12, min_ret=0.06, vol_mult=4.0, min_change=0.12, r1=0.5, r2=1.0, stop_atr=2.0,
                min_stop=0.025, trail=3.0, trail_after_r=1.0, atr_n=14, min_bars=250, max_per_day=1,
                market=False, longs=True, shorts=True, skip_hours=(), max_change=0.0,
                rungs=2, weights=None,
                # Conviction (0..1) for confidence-based sizing: averages three saturating gauges of
                # the ignition's quality. conf_ret = the 1h return that counts as full-strength impulse;
                # conf_vol = the volume multiple that counts as a full-strength surge.
                conf_ret=0.03, conf_vol=10.0)


class Strategy:
    def __init__(self, cfg, fees):
        self.cfg = cfg
        self.p = {**DEFAULTS, **cfg.params}
        self.book = make_book(cfg, self.p["max_per_day"])

    def on_bar(self, st):
        p = self.p
        bars = list(st.bars_e)
        ts = st.last_bar.close_time
        ch = st.change_24h
        n = p["ret_n"]
        if p["max_change"] and ch is not None and abs(ch) >= p["max_change"]:
            return None  # round 3: only fresh movers (|change_24h| below the cap at the signal)
        if ch is None or abs(ch) < p["min_change"] or len(bars) < p["min_bars"] or not self.book.can_trade(st.symbol, ts) \
                or not side_ok(p, 1 if ch > 0 else -1) or not hour_ok(p, ts):
            return None
        c = bars[-1].close
        c0 = bars[-n - 1].close
        if c0 <= 0:
            return None
        ret = c / c0 - 1
        prev = bars[:-1]
        if ch > 0 and ret >= p["min_ret"] and c > max(b.high for b in prev):
            s = 1
        elif ch < 0 and ret <= -p["min_ret"] and c < min(b.low for b in prev):
            s = -1
        else:
            return None
        hour_vol = sum(b.volume for b in bars[-n:])
        rest = bars[:-n]
        base = sum(b.volume for b in rest) / len(rest) * n
        if base <= 0 or hour_vol < p["vol_mult"] * base:
            return None
        # Conviction (0..1): strong impulse + big volume surge + a clean (non-choppy) run size up.
        volx = hour_vol / base
        er = efficiency_ratio([b.close for b in bars], n)      # 0 = chop, 1 = straight line
        sat = lambda x, ref: min(x / ref, 1.0) if ref > 0 else 1.0  # noqa: E731
        conf = (sat(abs(ret), p["conf_ret"]) + sat(volx, p["conf_vol"]) + er) / 3.0
        a = atr_bars(bars, p["atr_n"])
        if a <= 0:
            return None
        r2 = c - s * p["r2"] * a                       # deepest rung offset (the stop sits below it)
        n = 1 if p["market"] else max(1, int(p["rungs"]))
        if n <= 1:
            rungs, weights = [c], [1.0]                 # market / single: fill at the signal close
        else:
            # Spread n limit rungs from r1 (shallow) to r2 (deepest, by the stop) for a better average
            # entry; `weights` biases size toward the deeper rungs (deepest last; default equal).
            offs = [p["r1"] + (p["r2"] - p["r1"]) * i / (n - 1) for i in range(n)]
            rungs = [c - s * o * a for o in offs]
            weights = list(p["weights"]) if p["weights"] else [1.0] * n
            weights = (weights + [weights[-1]] * n)[:n]
        tw = sum(weights)
        avg = sum(px * w for px, w in zip(rungs, weights)) / tw
        stop = r2 - s * p["stop_atr"] * a
        if s * (avg - stop) / avg < p["min_stop"]:
            stop = avg * (1 - s * p["min_stop"])
        if stop <= 0:
            return None
        side = Side.LONG if s > 0 else Side.SHORT
        sig = signal(st.symbol, side, ts, c, stop, far_target(s, c, stop),
                     f"ignition 1h={ret:+.0%} vol={hour_vol / base:.1f}x ch={ch:+.0%}")
        if sig is not None:
            if n > 1:
                sig.ladder = [(px, w / tw) for px, w in zip(rungs, weights)]
            sig.confidence = conf
            self.book.record(st.symbol, ts)
        return sig

    def manage(self, st, pos):
        p = self.p
        risk = self.book.risk(st, pos)
        best = best_close(st, pos)
        a = atr_bars(list(st.bars_e), p["atr_n"])
        s = pos.side.sign
        if best is None or a <= 0 or risk <= 0 or s * (best - pos.entry_price) < p["trail_after_r"] * risk:
            return None
        return best - s * p["trail"] * a
