"""Post-blow-off failure short (15m): first close below VWAP24 hours after the high, sell the retest.

Up-mover with change_24h >= min_change whose 24h high is >= blowoff x VWAP24 and was set
`hi_age_min`..`hi_age_max` bars ago. Trigger: the first 15m close below VWAP24 since that high
(approximation: every close between the high and this bar was >= the current VWAP24) with volume
>= vol_mult x the `vol_n`-bar mean. Entry: sell limits at VWAP24 and VWAP24 + rung_atr x ATR15
(`market: true` = market at the signal). Stop = max(high of the last `stop_bars` bars,
rung2 + stop_atr x ATR15), distance in [min_stop, max_stop]. TP (maker) = avg entry - tp_r R.
manage(): break-even after +be_r R. `mirror: true` also trades the first VWAP reclaim (long) on
down-movers.
Hypothesis: pumps revert after they visibly fail; fading only after the VWAP loss avoids the
"fade into strength" losses of earlier searches.
"""

from __future__ import annotations

from autotr.models import Side
from autotr.strategy.research._common import signal
from autotr.strategy.research._scalp import make_book, atr_bars, best_extreme, vwap

DEFAULTS = dict(min_change=0.25, blowoff=1.12, hi_age_min=8, hi_age_max=64, vol_mult=1.0, vol_n=20,
                rung_atr=0.5, stop_bars=8, stop_atr=1.5, min_stop=0.02, max_stop=0.08, tp_r=2.0, be_r=1.0,
                atr_n=14, min_bars=90, max_per_day=1, market=False, mirror=False)


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
        if ch is None or len(bars) < p["min_bars"] or not self.book.can_trade(st.symbol, ts):
            return None
        if ch >= p["min_change"]:
            s = -1
        elif p["mirror"] and ch <= -p["min_change"]:
            s = 1
        else:
            return None
        vw = vwap(bars)
        a = atr_bars(bars, p["atr_n"])
        if vw is None or a <= 0:
            return None
        n = len(bars)
        if s < 0:
            i_ext = max(range(n), key=lambda i: bars[i].high)
            ext_ok = bars[i_ext].high >= vw * p["blowoff"]
        else:
            i_ext = min(range(n), key=lambda i: bars[i].low)
            ext_ok = bars[i_ext].low <= vw / p["blowoff"]
        age = n - 1 - i_ext
        if not ext_ok or not p["hi_age_min"] <= age <= p["hi_age_max"]:
            return None
        cur = bars[-1]
        between = bars[i_ext:-1]
        # first close through VWAP since the extreme
        if s < 0 and not (cur.close < vw and all(b.close >= vw for b in between)):
            return None
        if s > 0 and not (cur.close > vw and all(b.close <= vw for b in between)):
            return None
        vols = [b.volume for b in bars[-p["vol_n"] - 1:-1]]
        if cur.volume < p["vol_mult"] * sum(vols) / len(vols):
            return None
        c = cur.close
        ladder = [(vw, 0.5), (vw - s * p["rung_atr"] * a, 0.5)]
        r2 = ladder[-1][0]
        recent = bars[-p["stop_bars"]:]
        if s < 0:
            stop = max(max(b.high for b in recent), r2 + p["stop_atr"] * a)
        else:
            stop = min(min(b.low for b in recent), r2 - p["stop_atr"] * a)
        avg = c if p["market"] else (ladder[0][0] + ladder[1][0]) / 2
        dist = abs(avg - stop) / avg
        if not p["min_stop"] <= dist <= p["max_stop"]:
            return None
        tp = avg + s * p["tp_r"] * abs(avg - stop)
        if tp <= 0:
            return None
        side = Side.LONG if s > 0 else Side.SHORT
        sig = signal(st.symbol, side, ts, c, stop, tp, f"vwap fail ch={ch:+.0%} age={age}")
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
