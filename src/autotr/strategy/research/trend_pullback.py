"""Trend pullback: higher-TF (structure) trend by EMA fast>slow and rising; entry-TF RSI dips to
`rsi_dip` then turns back up → join the trend. Stop below the recent entry-TF swing, target R
multiple. Hypothesis: buying dips in an established trend beats fading moves."""

from __future__ import annotations

from autotr.models import Side
from autotr.strategy.research._common import atr, ema, rsi, signal

DEFAULTS = dict(ema_fast=20, ema_slow=50, rsi_dip=40, swing_bars=8, stop_buf_atr=0.3, tp_r=2.0)


class Strategy:
    def __init__(self, cfg, fees):
        self.cfg = cfg
        self.p = {**DEFAULTS, **cfg.params}

    def on_bar(self, st):
        p = self.p
        c = list(st.c_s)
        if len(c) < p["ema_slow"] + 5:
            return None
        ef, es = ema(c, p["ema_fast"]), ema(c, p["ema_slow"])
        up = ef[-1] > es[-1] and es[-1] > es[-4]
        dn = ef[-1] < es[-1] and es[-1] < es[-4]
        if not (up or dn):
            return None
        bars = list(st.bars_e)
        if len(bars) < 40:
            return None
        ec = [b.close for b in bars]
        eh = [b.high for b in bars]
        el = [b.low for b in bars]
        r_now, r_prev = rsi(ec), rsi(ec[:-1])
        a = atr(eh, el, ec)
        price = ec[-1]
        ts = st.last_bar.close_time
        k = p["swing_bars"]
        if up and r_prev <= p["rsi_dip"] < r_now:
            stop = min(el[-k:]) - p["stop_buf_atr"] * a
            return signal(st.symbol, Side.LONG, ts, price, stop, price + p["tp_r"] * (price - stop), "uptrend dip bought")
        if dn and r_prev >= 100 - p["rsi_dip"] > r_now:
            stop = max(eh[-k:]) + p["stop_buf_atr"] * a
            return signal(st.symbol, Side.SHORT, ts, price, stop, price - p["tp_r"] * (stop - price), "downtrend rally sold")
        return None
