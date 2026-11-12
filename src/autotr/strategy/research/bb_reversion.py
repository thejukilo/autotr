"""Bollinger mean reversion in ranging markets (structure-TF bars): close outside the k-sigma band
with RSI extreme while ADX is low → fade back to the middle band. Hypothesis: in non-trending
regimes, stretched moves revert; ADX filter avoids fading real trends."""

from __future__ import annotations

from autotr.models import Side
from autotr.strategy.research._common import adx, atr, rsi, signal, sma, stdev

DEFAULTS = dict(bb_n=20, bb_k=2.5, rsi_lo=25, rsi_hi=75, adx_max=22, stop_atr=1.5)


class Strategy:
    def __init__(self, cfg, fees):
        self.cfg = cfg
        self.p = {**DEFAULTS, **cfg.params}

    def on_bar(self, st):
        p = self.p
        c, h, lo = list(st.c_s), list(st.h_s), list(st.l_s)
        if len(c) < 60:
            return None
        mid, sd = sma(c, p["bb_n"]), stdev(c, p["bb_n"])
        price = c[-1]
        if adx(h[-100:], lo[-100:], c[-100:]) > p["adx_max"]:
            return None
        r = rsi(c[-100:])
        a = atr(h, lo, c)
        ts = st.last_bar.close_time
        if price < mid - p["bb_k"] * sd and r <= p["rsi_lo"]:
            return signal(st.symbol, Side.LONG, ts, price, price - p["stop_atr"] * a, mid, f"BB fade low, RSI {r:.0f}")
        if price > mid + p["bb_k"] * sd and r >= p["rsi_hi"]:
            return signal(st.symbol, Side.SHORT, ts, price, price + p["stop_atr"] * a, mid, f"BB fade high, RSI {r:.0f}")
        return None
