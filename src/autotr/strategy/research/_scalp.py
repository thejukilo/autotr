"""Shared helpers for the scalping-on-movers strategies (`scalp_*.py`).

Everything works on `st.bars_e` (entry-timeframe bars, which carry volume) so a rolling VWAP and
volume baselines are available. `Book` keeps per-strategy bookkeeping:

* a per-symbol, per-UTC-day entry cap (counted when a signal is emitted; a signal the risk manager
  rejects still counts, which is a conservative approximation);
* "in position" detection: the engine calls `manage` for an open position *before* `on_bar` on the
  same candle, so `Book.touch` in `manage` lets `on_bar` stay silent while a trade is open (otherwise
  rejected "already_in_symbol" signals would eat the daily cap and overwrite per-trade metadata);
* the initial stop of each position (first value seen by `manage`, before any tightening) so that
  R-multiples for break-even / trailing rules use the original risk.
"""

from __future__ import annotations

import statistics
from typing import Sequence

from autotr.models import DAY_MS, Bar


def vwap(bars: Sequence[Bar]) -> float | None:
    vol = sum(b.volume for b in bars)
    if vol <= 0:
        return None
    return sum(b.quote_volume for b in bars) / vol


def atr_bars(bars: Sequence[Bar], n: int = 14) -> float:
    bars = list(bars)[-(n + 1):]
    if not bars:
        return 0.0
    trs = [bars[0].high - bars[0].low] if len(bars) <= n else []
    for i in range(1, len(bars)):
        b, pc = bars[i], bars[i - 1].close
        trs.append(max(b.high - b.low, abs(b.high - pc), abs(b.low - pc)))
    return sum(trs) / len(trs)


def ema_last(xs: Sequence[float], n: int) -> float:
    k = 2 / (n + 1)
    e = xs[0]
    for x in xs[1:]:
        e += k * (x - e)
    return e


def median(xs: Sequence[float]) -> float:
    return statistics.median(xs) if xs else 0.0


def since_entry(st, pos, strict: bool = False) -> list[Bar]:
    """Entry-TF bars after the first fill. strict=False includes the bar the fill happened in (its
    close is after the fill; its high/low may not be)."""
    lo = pos.entry_ts if strict else pos.entry_ts - st.entry_ms + 1
    return [b for b in st.bars_e if b.open_time >= lo]


def best_close(st, pos) -> float | None:
    bs = since_entry(st, pos)
    if not bs:
        return None
    return max(b.close for b in bs) if pos.side.sign > 0 else min(b.close for b in bs)


def best_extreme(st, pos) -> float | None:
    """Highest high (long) / lowest low (short) of bars that opened after the first fill."""
    bs = since_entry(st, pos, strict=True)
    if not bs:
        return None
    return max(b.high for b in bs) if pos.side.sign > 0 else min(b.low for b in bs)


def far_target(side_sign: int, price: float, stop: float, r: float = 50.0) -> float:
    """'No take-profit': a target so far away that it never fills (signal() needs one)."""
    tp = price + side_sign * r * abs(price - stop)
    return tp if side_sign > 0 else max(tp, price * 0.02)


class Book:
    def __init__(self, max_per_day: int = 0, block_ms: int = 0):
        self.block_ms = block_ms
        self.max_per_day = max_per_day
        self._count: dict[tuple[str, int], int] = {}
        self._seen: dict[str, int] = {}
        self._block: dict[str, int] = {}
        self._init_stop: dict[tuple[str, int], float] = {}
        self.meta: dict[str, dict] = {}

    # -- daily cap
    def can_trade(self, symbol: str, ts: int) -> bool:
        if self._seen.get(symbol) == ts or ts < self._block.get(symbol, 0):
            return False  # position open on this candle
        if self.max_per_day <= 0:
            return True
        return self._count.get((symbol, ts // DAY_MS), 0) < self.max_per_day

    def record(self, symbol: str, ts: int, block_ms: int | None = None, **meta) -> None:
        """Count an entry; `block_ms` = no new signal while the ladder may still be pending."""
        self._block[symbol] = ts + (self.block_ms if block_ms is None else block_ms)
        k = (symbol, ts // DAY_MS)
        self._count[k] = self._count.get(k, 0) + 1
        self.meta[symbol] = meta

    # -- position tracking (call from manage)
    def touch(self, st, pos) -> float:
        """Mark the symbol as in a position for this candle; return the initial stop."""
        self._seen[st.symbol] = st.last_bar.close_time
        return self._init_stop.setdefault((st.symbol, pos.entry_ts), pos.stop)

    def risk(self, st, pos) -> float:
        """Initial risk per unit (distance from the average entry to the initial stop)."""
        return abs(pos.entry_price - self.touch(st, pos))


def make_book(cfg, max_per_day: int = 0) -> Book:
    """Book whose post-signal block lasts as long as a ladder can stay pending."""
    return Book(max_per_day, cfg.ladder_expiry_bars * cfg.entry_tf_minutes * 60_000)


def side_ok(p: dict, s: int) -> bool:
    """`longs` / `shorts` params (default both on)."""
    return p.get("longs", True) if s > 0 else p.get("shorts", True)


def hour_ok(p: dict, ts: int) -> bool:
    """False if the UTC hour of `ts` (signal candle close) is in `skip_hours` (list of ints)."""
    skip = p.get("skip_hours") or ()
    return (ts // 3_600_000) % 24 not in skip  # close_time = open + tf = next entry time
