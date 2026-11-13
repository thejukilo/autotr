"""Cross-sectional intraday momentum inside the mover set (15m), market entry, chandelier trail.

Researcher candidate #7: the 4h cross-sectional momentum edge that survived holdout, shortened to
an intraday horizon and restricted to the day's movers.
* `observe()` sees every symbol on each 15m candle and stores score = `lb`-bar return /
  (ATR15 / price) for symbols with |change_24h| >= min_change and 24h quote volume >= min_qv
  (an approximation of the scanner's mover set; the strategy cannot see the scanner itself).
* Only on rebalance candles (UTC hour % `every_h` == 0, minute 0) and only for scores updated on
  this or the previous candle (symbols are processed one by one, so some scores are 15m stale).
* Long the top-`k` if score > 0 and close > VWAP24; short the bottom-`k` if score < 0 and close <
  VWAP24. Needs at least `min_coins` scored symbols. Market entry.
* Stop `stop_atr` x ATR15 (at least `min_stop`); no TP; manage() chandelier `trail` x ATR15 from
  the best close since entry; time stop from the config.
Hypothesis: relative strength among the day's movers persists for a few hours (intraday momentum,
Caporale & Plastun 2020) and an uncapped trail keeps the profit tail.
"""

from __future__ import annotations

from autotr.models import Side
from autotr.strategy.research._common import signal
from autotr.strategy.research._scalp import make_book, atr_bars, best_close, far_target, side_ok, vwap

DEFAULTS = dict(lb=16, every_h=4, k=1, min_change=0.10, min_qv=50_000_000, min_coins=4, stop_atr=3.0,
                min_stop=0.02, trail=3.0, atr_n=14, vwap_filter=True, max_per_day=2, longs=True, shorts=True)

H_MS = 3_600_000


class Strategy:
    def __init__(self, cfg, fees):
        self.cfg = cfg
        self.p = {**DEFAULTS, **cfg.params}
        self.book = make_book(cfg, self.p["max_per_day"])
        self.score: dict[str, tuple[int, float]] = {}  # symbol -> (candle close ts, score)

    def observe(self, st):
        p = self.p
        bars = list(st.bars_e)
        ch = st.change_24h
        n = p["lb"]
        if ch is None or abs(ch) < p["min_change"] or st.quote_volume_24h < p["min_qv"] or len(bars) < max(n + 1, p["atr_n"] + 1):
            self.score.pop(st.symbol, None)
            return
        c, c0 = bars[-1].close, bars[-n - 1].close
        a = atr_bars(bars, p["atr_n"])
        if c0 <= 0 or a <= 0:
            return
        self.score[st.symbol] = (st.last_bar.close_time, (c / c0 - 1) / (a / c))

    def on_bar(self, st):
        p = self.p
        ts = st.last_bar.close_time
        if ts % (p["every_h"] * H_MS) != 0 or not self.book.can_trade(st.symbol, ts) or st.symbol not in self.score:
            return None
        fresh = {s: v for s, (t, v) in self.score.items() if ts - t <= st.entry_ms}
        if len(fresh) < p["min_coins"] or st.symbol not in fresh:
            return None
        ranked = sorted(fresh, key=fresh.get, reverse=True)
        m = fresh[st.symbol]
        k = p["k"]
        if st.symbol in ranked[:k] and m > 0:
            s = 1
        elif st.symbol in ranked[-k:] and m < 0:
            s = -1
        else:
            return None
        if not side_ok(p, s):
            return None
        bars = list(st.bars_e)
        c = bars[-1].close
        vw = vwap(bars)
        if p["vwap_filter"] and (vw is None or s * (c - vw) <= 0):
            return None
        a = atr_bars(bars, p["atr_n"])
        dist = max(p["stop_atr"] * a / c, p["min_stop"])
        stop = c * (1 - s * dist)
        side = Side.LONG if s > 0 else Side.SHORT
        sig = signal(st.symbol, side, ts, c, stop, far_target(s, c, stop),
                     f"xs intraday score={m:+.1f} rank={ranked.index(st.symbol) + 1}/{len(ranked)}")
        if sig is not None:
            self.book.record(st.symbol, ts)
        return sig

    def manage(self, st, pos):
        self.book.touch(st, pos)
        best = best_close(st, pos)
        a = atr_bars(list(st.bars_e), self.p["atr_n"])
        if best is None or a <= 0:
            return None
        return best - pos.side.sign * self.p["trail"] * a
