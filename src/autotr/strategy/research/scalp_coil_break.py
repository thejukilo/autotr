"""Post-move coil (flag) breakout on 15m with a chandelier trail. Market entry, no target.

Up-mover (change_24h >= min_change), long only (mirror for down-movers):
* coil = the `coil_n` bars before the signal bar; range R = maxH - minL must be
  <= min(coil_atr x ATR15, coil_move_frac x |close - close `move_lb` bars ago|);
* coil low above the `coil_pos` level of the 24h range (the flag sits near the highs);
* trigger: close > coil high + brk_atr x ATR15 with volume >= vol_mult x the `vol_n`-bar mean.
Stop: coil low - stop_buf x ATR (coil midpoint if R/price > `wide`); stop distance must be in
[min_stop, max_stop]. manage(): chandelier `trail` x ATR15 from the best close, `trail_tight` x ATR
once the trade is +`tight_after_r` R.
Hypothesis: continuation on abnormal-return days; a structural, wide stop keeps fees < 0.1R and an
uncapped trail keeps the profit tail (the only edge the momentum search found).
Round 2 params: `longs` / `shorts` (side filter), `skip_hours` (UTC hours of the signal candle close).
"""

from __future__ import annotations

from autotr.models import Side
from autotr.strategy.research._common import signal
from autotr.strategy.research._scalp import make_book, atr_bars, best_close, far_target, hour_ok, side_ok

DEFAULTS = dict(min_change=0.20, coil_n=12, coil_atr=3.5, coil_move_frac=0.35, move_lb=96, coil_pos=0.6,
                brk_atr=0.1, vol_mult=1.5, vol_n=20, stop_buf=0.2, wide=0.06, min_stop=0.02, max_stop=0.07,
                trail=3.0, trail_tight=2.0, tight_after_r=1.5, atr_n=14, max_per_day=1,
                longs=True, shorts=True, skip_hours=())


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
        n = p["coil_n"]
        if ch is None or abs(ch) < p["min_change"] or len(bars) < max(p["move_lb"], n + p["vol_n"] + 1) \
                or not self.book.can_trade(st.symbol, ts) or not side_ok(p, 1 if ch > 0 else -1) \
                or not hour_ok(p, ts):
            return None
        cur = bars[-1]
        coil = bars[-n - 1:-1]
        ch_hi, ch_lo = max(b.high for b in coil), min(b.low for b in coil)
        r = ch_hi - ch_lo
        a = atr_bars(bars[:-1], p["atr_n"])
        move = abs(cur.close - bars[-p["move_lb"]].close)
        if a <= 0 or r > min(p["coil_atr"] * a, p["coil_move_frac"] * move):
            return None
        vols = [b.volume for b in bars[-p["vol_n"] - 1:-1]]
        if cur.volume < p["vol_mult"] * sum(vols) / len(vols):
            return None
        d_hi = max(b.high for b in bars[-p["move_lb"]:])
        d_lo = min(b.low for b in bars[-p["move_lb"]:])
        c = cur.close
        if ch > 0 and ch_lo >= d_lo + p["coil_pos"] * (d_hi - d_lo) and c > ch_hi + p["brk_atr"] * a:
            stop = (ch_hi + ch_lo) / 2 if r / c > p["wide"] else ch_lo - p["stop_buf"] * a
            side = Side.LONG
        elif ch < 0 and ch_hi <= d_hi - p["coil_pos"] * (d_hi - d_lo) and c < ch_lo - p["brk_atr"] * a:
            stop = (ch_hi + ch_lo) / 2 if r / c > p["wide"] else ch_hi + p["stop_buf"] * a
            side = Side.SHORT
        else:
            return None
        dist = abs(c - stop) / c
        if not p["min_stop"] <= dist <= p["max_stop"]:
            return None
        sig = signal(st.symbol, side, ts, c, stop, far_target(side.sign, c, stop),
                     f"coil break R={r / c:.1%} ch={ch:+.0%}")
        if sig is not None:
            self.book.record(st.symbol, ts)
        return sig

    def manage(self, st, pos):
        p = self.p
        risk = self.book.risk(st, pos)
        best = best_close(st, pos)
        a = atr_bars(list(st.bars_e), p["atr_n"])
        if best is None or a <= 0:
            return None
        s = pos.side.sign
        mult = p["trail_tight"] if risk > 0 and s * (best - pos.entry_price) >= p["tight_after_r"] * risk else p["trail"]
        return best - s * mult * a
