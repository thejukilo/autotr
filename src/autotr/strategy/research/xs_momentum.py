"""Cross-sectional momentum (4h bars): rank all coins by their `lookback`-bar return; go long the
top `k` when above their EMA, short the bottom `k` when below it. Wide ATR stop, R target, long
time stop. Hypothesis: relative strength persists for days in crypto; very few trades → little
fee drag. Uses the `observe` hook to see every coin, not just the scanner's picks."""

from __future__ import annotations

from autotr.models import Side
from autotr.strategy.research._common import atr, ema, signal

DEFAULTS = dict(lookback=42, k=3, ema=50, stop_atr=2.5, tp_r=3.0, min_coins=10, longs=True, shorts=True)


class Strategy:
    def __init__(self, cfg, fees):
        self.cfg = cfg
        self.p = {**DEFAULTS, **cfg.params}
        self.mom: dict[str, float] = {}

    def observe(self, st):
        c = st.c_s
        n = self.p["lookback"]
        if len(c) > n and c[-n - 1] > 0:
            self.mom[st.symbol] = c[-1] / c[-n - 1] - 1

    def on_bar(self, st):
        p = self.p
        if st.symbol not in self.mom or len(self.mom) < p["min_coins"]:
            return None
        ranked = sorted(self.mom, key=self.mom.get, reverse=True)
        c, h, lo = list(st.c_s), list(st.h_s), list(st.l_s)
        if len(c) < p["ema"]:
            return None
        price = c[-1]
        # ema <= 1 disables the trend filter
        e = ema(c, p["ema"])[-1] if p["ema"] > 1 else None
        a = atr(h, lo, c)
        price = c[-1]
        ts = st.last_bar.close_time
        m = self.mom[st.symbol]
        if p["longs"] and st.symbol in ranked[: p["k"]] and (e is None or price > e) and m > 0:
            stop = price - p["stop_atr"] * a
            return signal(st.symbol, Side.LONG, ts, price, stop, price + p["tp_r"] * (price - stop),
                          f"top-{p['k']} momentum {m:+.0%}")
        if p["shorts"] and st.symbol in ranked[-p["k"]:] and (e is None or price < e) and m < 0:
            stop = price + p["stop_atr"] * a
            return signal(st.symbol, Side.SHORT, ts, price, stop, price - p["tp_r"] * (stop - price),
                          f"bottom-{p['k']} momentum {m:+.0%}")
        return None
