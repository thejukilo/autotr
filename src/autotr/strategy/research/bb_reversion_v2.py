"""Bollinger reversion v2 (round 2, from tester-B findings on bb_reversion):
* wait for the turn: previous bar closed outside the k-sigma band, current bar closes back inside;
* wider stop: beyond the excursion extreme + buffer, at least `min_stop_atr` ATR (v1 stops were hit
  within 2h while >6h holds were profitable);
* chop filter: efficiency ratio over `er_n` bars below `er_max` (instead of ADX alone).
Target: middle band."""

from __future__ import annotations

from autotr.models import Side
from autotr.strategy.research._common import atr, efficiency_ratio, rsi, signal, sma, stdev

DEFAULTS = dict(bb_n=20, bb_k=2.5, rsi_lo=30, rsi_hi=70, er_n=48, er_max=0.3, buf_atr=0.5,
                min_stop_atr=2.0, min_rr=0.8)


class Strategy:
    def __init__(self, cfg, fees):
        self.cfg = cfg
        self.p = {**DEFAULTS, **cfg.params}

    def on_bar(self, st):
        p = self.p
        c, h, lo = list(st.c_s), list(st.h_s), list(st.l_s)
        n = p["bb_n"]
        if len(c) < 100:
            return None
        if efficiency_ratio(c, p["er_n"]) > p["er_max"]:
            return None
        mid, sd = sma(c, n), stdev(c, n)
        pmid, psd = sma(c[:-1], n), stdev(c[:-1], n)
        price = c[-1]
        a = atr(h, lo, c)
        r_prev = rsi(c[:-1][-100:])
        ts = st.last_bar.close_time
        if c[-2] < pmid - p["bb_k"] * psd and price > mid - p["bb_k"] * sd and r_prev <= p["rsi_lo"]:
            stop = min(min(lo[-3:]) - p["buf_atr"] * a, price - p["min_stop_atr"] * a)
            if mid - price >= p["min_rr"] * (price - stop):
                return signal(st.symbol, Side.LONG, ts, price, stop, mid, f"BB re-entry from below, RSI {r_prev:.0f}")
        if c[-2] > pmid + p["bb_k"] * psd and price < mid + p["bb_k"] * sd and r_prev >= p["rsi_hi"]:
            stop = max(max(h[-3:]) + p["buf_atr"] * a, price + p["min_stop_atr"] * a)
            if price - mid >= p["min_rr"] * (stop - price):
                return signal(st.symbol, Side.SHORT, ts, price, stop, mid, f"BB re-entry from above, RSI {r_prev:.0f}")
        return None
