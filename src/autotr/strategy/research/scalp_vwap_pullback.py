"""Trend-of-day pullback to the EMA/VWAP zone (5m), limit ladder in, maker TP at the recent high.

Up-mover (change_24h >= min_change), long (mirror for down-movers):
* trend: close > VWAP24 (rolling, from 288 x 5m bars), EMA20 > EMA50 on 5m, last structure (15m)
  close > EMA20(15m), and a new 24h high within the last `new_high_bars` bars;
* setup: close <= `hh_bars`-bar high - pull_atr x ATR5 and still above VWAP24.
Entry: limits at close - r1 x ATR5 and max(close - r2 x ATR5, VWAP24 x (1 + vwap_buf)).
Stop = min(rung2 - stop_atr x ATR5, VWAP24 - vwap_stop_atr x ATR5), distance from the average rung
in [min_stop, max_stop]. TP (maker) = recent high + tp_atr x ATR5, only if reward/risk >= min_rr.
manage(): break-even after +be_r R. `market: true` = the market-entry twin (same levels, entry at
the signal close) to measure adverse selection of the ladder.
Hypothesis: buying pullbacks on the day's trend with maker legs captures the retest of the high
at ~0.01-0.03R cost.
Round 2: `single: true` = one rung at close - r1 x ATR5 (no VWAP rung; the stop is then measured
from that rung); `longs` / `shorts`, `skip_hours` filters.
"""

from __future__ import annotations

from autotr.models import Side
from autotr.strategy.research._common import signal
from autotr.strategy.research._scalp import make_book, atr_bars, best_extreme, ema_last, vwap, hour_ok, side_ok

DEFAULTS = dict(min_change=0.15, ema_f=20, ema_s=50, s_ema=20, new_high_bars=24, hh_bars=24, pull_atr=1.5,
                r1=0.5, r2=1.25, vwap_buf=0.002, stop_atr=1.5, vwap_stop_atr=0.5, min_stop=0.015,
                max_stop=0.06, tp_atr=0.25, min_rr=1.0, be_r=1.0, atr_n=14, min_bars=250, max_per_day=2,
                market=False, single=False, longs=True, shorts=True, skip_hours=())


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
        if ch is None or abs(ch) < p["min_change"] or len(bars) < p["min_bars"] or len(st.c_s) < p["s_ema"] \
                or not self.book.can_trade(st.symbol, ts) or not side_ok(p, 1 if ch > 0 else -1) \
                or not hour_ok(p, ts):
            return None
        vw = vwap(bars)
        a = atr_bars(bars, p["atr_n"])
        if vw is None or a <= 0:
            return None
        closes = [b.close for b in bars]
        c = closes[-1]
        ef, es = ema_last(closes, p["ema_f"]), ema_last(closes, p["ema_s"])
        se = ema_last(list(st.c_s), p["s_ema"])
        sc = st.c_s[-1]
        recent = bars[-p["new_high_bars"]:]
        hh_win = bars[-p["hh_bars"]:]
        s = 1 if ch > 0 else -1
        if s > 0:
            ok = (c > vw and ef > es and sc > se and max(b.high for b in recent) >= max(b.high for b in bars))
            ext = max(b.high for b in hh_win)
            ok = ok and c <= ext - p["pull_atr"] * a
        else:
            ok = (c < vw and ef < es and sc < se and min(b.low for b in recent) <= min(b.low for b in bars))
            ext = min(b.low for b in hh_win)
            ok = ok and c >= ext + p["pull_atr"] * a
        if not ok:
            return None
        r1 = c - s * p["r1"] * a
        r2 = c - s * p["r2"] * a
        r2 = max(r2, vw * (1 + p["vwap_buf"])) if s > 0 else min(r2, vw * (1 - p["vwap_buf"]))
        ladder = [(r1, 0.5), (r2, 0.5)] if s * (r1 - r2) > 0 and not p["single"] else [(r1, 1.0)]
        deepest = ladder[-1][0]
        if s > 0:
            stop = min(deepest - p["stop_atr"] * a, vw - p["vwap_stop_atr"] * a)
        else:
            stop = max(deepest + p["stop_atr"] * a, vw + p["vwap_stop_atr"] * a)
        avg = c if p["market"] else sum(px * w for px, w in ladder)
        dist = s * (avg - stop) / avg
        if not p["min_stop"] <= dist <= p["max_stop"]:
            return None
        tp = ext + s * p["tp_atr"] * a
        if s * (tp - avg) < p["min_rr"] * s * (avg - stop):
            return None
        side = Side.LONG if s > 0 else Side.SHORT
        sig = signal(st.symbol, side, ts, c, stop, tp, f"vwap pullback ch={ch:+.0%} vw={vw:.6g}")
        if sig is not None:
            if not p["market"]:
                sig.ladder = ladder
            self.book.record(st.symbol, ts)
        return sig

    def manage(self, st, pos):
        risk = self.book.risk(st, pos)
        ext = best_extreme(st, pos)
        if ext is not None and risk > 0 and pos.side.sign * (ext - pos.entry_price) >= self.p["be_r"] * risk:
            return pos.entry_price
        return None
