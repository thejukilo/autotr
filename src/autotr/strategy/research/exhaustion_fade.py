"""Exhaustion fade (structure-TF bars): coin up/down > `min_move` over 24h with structure RSI beyond
`rsi_ext`, and a reversal bar (close beyond the previous bar's low/high) → fade toward the EMA.
A slower, stricter version of the manual 24h-mover method: wait for the turn, then fade."""

from __future__ import annotations

from autotr.models import Side
from autotr.strategy.research._common import ema, rsi, signal

DEFAULTS = dict(min_move=0.08, rsi_ext=78, ema=20, stop_buf=0.002, min_rr=1.2)


class Strategy:
    def __init__(self, cfg, fees):
        self.cfg = cfg
        self.p = {**DEFAULTS, **cfg.params}

    def on_bar(self, st):
        p = self.p
        c, h, lo = list(st.c_s), list(st.h_s), list(st.l_s)
        chg = st.change_24h
        if len(c) < 60 or chg is None:
            return None
        r = rsi(c[:-1][-100:])          # RSI at the extreme bar, before the reversal bar
        e = ema(c, p["ema"])[-1]
        price = c[-1]
        ts = st.last_bar.close_time
        if chg > p["min_move"] and r >= p["rsi_ext"] and price < lo[-2]:
            stop = max(h[-3:]) * (1 + p["stop_buf"])
            if (price - e) >= p["min_rr"] * (stop - price):
                return signal(st.symbol, Side.SHORT, ts, price, stop, e, f"24h +{chg:.0%}, RSI {r:.0f}, reversal bar")
        if chg < -p["min_move"] and r <= 100 - p["rsi_ext"] and price > h[-2]:
            stop = min(lo[-3:]) * (1 - p["stop_buf"])
            if (e - price) >= p["min_rr"] * (price - stop):
                return signal(st.symbol, Side.LONG, ts, price, stop, e, f"24h {chg:.0%}, RSI {r:.0f}, reversal bar")
        return None
