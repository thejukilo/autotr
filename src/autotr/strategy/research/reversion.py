"""Mean-reversion scalper: fade an over-extension back to the mean. PAPER / RESEARCH.

The opposite bet to scalp_ignition. Movers over-shoot; this buys the dip / sells the rip when price
is stretched `band_k` bands away from a moving mean, and targets the mean. Designed to (a) trade more
often (every stretch, not just fresh breakouts) and (b) pay MAKER fees by entering with a resting
limit order (market: false) instead of chasing — the two things fees punish least.

Params:
  mean   = ema | sma | vwap     the reference mean (mean_n bars)
  band   = stdev | atr          how "stretched" is measured; band_k = how many bands triggers
  rsi_gate (0/1)                 also require RSI(entry) < rsi_low (long) / > rsi_high (short)
  trend_n (0 = off)             only fade WITH a longer EMA(trend_n) trend (Connors-style filter)
  stop_atr                       stop = stop_atr x ATR beyond entry
  take_frac                      target = entry + take_frac x (mean - entry); 1.0 = the mean
  market (false = limit/maker)   entry order type
"""

from __future__ import annotations

from autotr.models import Side
from autotr.strategy.research._common import efficiency_ratio, ema, sma, signal, stdev
from autotr.strategy.research._scalp import hour_ok, make_book, side_ok, vwap

DEFAULTS = dict(mean="ema", mean_n=20, band="stdev", band_k=2.0, rsi_gate=0, rsi_low=25, rsi_high=75,
                stop_atr=1.5, take_frac=1.0, trend_n=0, min_change=0.0, max_change=0.0, min_bars=30,
                chop_n=0, chop_max=1.0, market=False, longs=True, shorts=True, max_per_day=200, skip_hours=())


class Strategy:
    def __init__(self, cfg, fees):
        self.cfg = cfg
        self.p = {**DEFAULTS, **cfg.params}
        self.book = make_book(cfg, self.p["max_per_day"])

    def on_bar(self, st):
        p = self.p
        bars = list(st.bars_e)
        if len(bars) < max(p["mean_n"] + 2, p["min_bars"]):
            return None
        ts = st.last_bar.close_time
        if not self.book.can_trade(st.symbol, ts) or not hour_ok(p, ts):
            return None
        ch = st.change_24h
        if p["min_change"] and (ch is None or abs(ch) < p["min_change"]):
            return None
        if p["max_change"] and ch is not None and abs(ch) >= p["max_change"]:
            return None                               # skip the day's big trenders (fade RANGING only)
        closes = [b.close for b in bars]
        c = closes[-1]
        if p["chop_n"] and efficiency_ratio(closes, p["chop_n"]) > p["chop_max"]:
            return None                               # skip clean trends; fade only choppy price
        if p["mean"] == "vwap":
            m = vwap(bars[-p["mean_n"]:]) or sma(closes, p["mean_n"])
        elif p["mean"] == "sma":
            m = sma(closes, p["mean_n"])
        else:
            m = ema(closes, p["mean_n"])[-1]
        atr = st.atr_e.value
        band = (atr or 0.0) * p["band_k"] if p["band"] == "atr" else stdev(closes, p["mean_n"]) * p["band_k"]
        if m <= 0 or band <= 0 or not atr:
            return None
        dev = c - m
        rsi = st.rsi_now
        if dev < -band and (not p["rsi_gate"] or (rsi is not None and rsi < p["rsi_low"])):
            s = 1
        elif dev > band and (not p["rsi_gate"] or (rsi is not None and rsi > p["rsi_high"])):
            s = -1
        else:
            return None
        if not side_ok(p, s):
            return None
        if p["trend_n"]:
            lt = ema(closes, p["trend_n"])[-1]
            if (s > 0 and c < lt) or (s < 0 and c > lt):
                return None
        stop = c - s * p["stop_atr"] * atr
        tp = c + s * p["take_frac"] * abs(m - c)      # target = the mean (take_frac 1.0)
        side = Side.LONG if s > 0 else Side.SHORT
        sig = signal(st.symbol, side, ts, c, stop, tp, f"revert {dev / m:+.1%}")
        if sig is not None:
            if not p["market"]:
                sig.ladder = [(c, 1.0)]               # resting limit (maker) at the stretched price
            self.book.record(st.symbol, ts)
        return sig
