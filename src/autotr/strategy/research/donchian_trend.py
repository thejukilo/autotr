"""Donchian trend breakout (structure-TF bars): close beyond the prior N-bar high/low, in the
direction of a slow EMA. Stop k×ATR, target R multiple, long time stop. Hypothesis: crypto trends
persist after range breaks; few trades → low fee drag."""

from __future__ import annotations

from autotr.models import Side
from autotr.strategy.research._common import atr, ema, signal

DEFAULTS = dict(lookback=48, ema=100, stop_atr=2.0, tp_r=2.5, ema_filter=True)


class Strategy:
    def __init__(self, cfg, fees):
        self.cfg = cfg
        self.p = {**DEFAULTS, **cfg.params}

    def on_bar(self, st):
        p = self.p
        n = p["lookback"]
        c, h, lo = list(st.c_s), list(st.h_s), list(st.l_s)
        if len(c) < max(n, p["ema"]) + 2:
            return None
        price = c[-1]
        hi_prev, lo_prev = max(h[-n - 1:-1]), min(lo[-n - 1:-1])
        a = atr(h, lo, c, 14)
        e = ema(c, p["ema"])[-1]
        ts = st.last_bar.close_time
        if price > hi_prev and c[-2] <= hi_prev and (not p["ema_filter"] or price > e):
            stop = price - p["stop_atr"] * a
            return signal(st.symbol, Side.LONG, ts, price, stop, price + p["tp_r"] * (price - stop),
                          f"donchian {n} breakout up")
        if price < lo_prev and c[-2] >= lo_prev and (not p["ema_filter"] or price < e):
            stop = price + p["stop_atr"] * a
            return signal(st.symbol, Side.SHORT, ts, price, stop, price - p["tp_r"] * (stop - price),
                          f"donchian {n} breakout down")
        return None
