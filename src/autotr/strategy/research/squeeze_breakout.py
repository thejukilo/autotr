"""Volatility squeeze breakout (structure-TF bars): Bollinger width in the lowest `pct` of the last
`hist` bars, then a close outside the band → trade the expansion. Hypothesis: compression precedes
directional expansion; trading only after quiet periods keeps the count low."""

from __future__ import annotations

from autotr.models import Side
from autotr.strategy.research._common import atr, signal, sma, stdev

DEFAULTS = dict(bb_n=20, bb_k=2.0, hist=200, pct=0.15, stop_atr=1.5, tp_r=2.5, squeeze_within=6)


class Strategy:
    def __init__(self, cfg, fees):
        self.cfg = cfg
        self.p = {**DEFAULTS, **cfg.params}

    def on_bar(self, st):
        p = self.p
        c, h, lo = list(st.c_s), list(st.h_s), list(st.l_s)
        n = p["bb_n"]
        if len(c) < p["hist"] + n:
            return None
        widths = [stdev(c[: i + 1], n) / sma(c[: i + 1], n) for i in range(len(c) - p["hist"], len(c))]
        cut = sorted(widths)[int(len(widths) * p["pct"])]
        if min(widths[-p["squeeze_within"] - 1:-1]) > cut:
            return None
        mid, sd = sma(c, n), stdev(c, n)
        price = c[-1]
        a = atr(h, lo, c)
        pmid, psd = sma(c[:-1], n), stdev(c[:-1], n)
        ts = st.last_bar.close_time
        if price > mid + p["bb_k"] * sd and c[-2] <= pmid + p["bb_k"] * psd:
            stop = price - p["stop_atr"] * a
            return signal(st.symbol, Side.LONG, ts, price, stop, price + p["tp_r"] * (price - stop), "squeeze break up")
        if price < mid - p["bb_k"] * sd and c[-2] >= pmid - p["bb_k"] * psd:
            stop = price + p["stop_atr"] * a
            return signal(st.symbol, Side.SHORT, ts, price, stop, price - p["tp_r"] * (stop - price), "squeeze break down")
        return None
