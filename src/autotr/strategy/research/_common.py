"""Small indicator helpers over plain lists (oldest first) for research strategies."""

from __future__ import annotations

import math
from typing import Sequence

from autotr.models import Side, Signal


def ema(xs: Sequence[float], n: int) -> list[float]:
    if not xs:
        return []
    k = 2 / (n + 1)
    out = [xs[0]]
    for x in xs[1:]:
        out.append(out[-1] + k * (x - out[-1]))
    return out


def sma(xs: Sequence[float], n: int) -> float:
    return sum(xs[-n:]) / min(n, len(xs))


def stdev(xs: Sequence[float], n: int) -> float:
    w = xs[-n:]
    m = sum(w) / len(w)
    return math.sqrt(sum((x - m) ** 2 for x in w) / len(w))


def true_ranges(h: Sequence[float], lo: Sequence[float], c: Sequence[float]) -> list[float]:
    out = [h[0] - lo[0]]
    for i in range(1, len(c)):
        out.append(max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1])))
    return out


def atr(h: Sequence[float], lo: Sequence[float], c: Sequence[float], n: int = 14) -> float:
    tr = true_ranges(h, lo, c)
    return sum(tr[-n:]) / min(n, len(tr))


def rsi(c: Sequence[float], n: int = 14) -> float:
    """Wilder RSI of the last value."""
    if len(c) < n + 1:
        return 50.0
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = c[i] - c[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def adx(h: Sequence[float], lo: Sequence[float], c: Sequence[float], n: int = 14) -> float:
    """Wilder ADX of the last value (simple, O(len))."""
    if len(c) < 2 * n + 1:
        return 0.0
    tr_s = pdm_s = mdm_s = 0.0
    dxs: list[float] = []
    for i in range(1, len(c)):
        up, dn = h[i] - h[i - 1], lo[i - 1] - lo[i]
        pdm = up if up > dn and up > 0 else 0.0
        mdm = dn if dn > up and dn > 0 else 0.0
        tr = max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1]))
        if i <= n:
            tr_s += tr
            pdm_s += pdm
            mdm_s += mdm
            if i < n:
                continue
        else:
            tr_s = tr_s - tr_s / n + tr
            pdm_s = pdm_s - pdm_s / n + pdm
            mdm_s = mdm_s - mdm_s / n + mdm
        if tr_s <= 0:
            continue
        pdi, mdi = 100 * pdm_s / tr_s, 100 * mdm_s / tr_s
        dxs.append(100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi > 0 else 0.0)
    if len(dxs) < n:
        return 0.0
    a = sum(dxs[:n]) / n
    for d in dxs[n:]:
        a = (a * (n - 1) + d) / n
    return a


def signal(symbol: str, side: Side, ts: int, price: float, stop: float, tp: float, reason: str) -> Signal | None:
    s = side.sign
    if s * (price - stop) <= 0 or s * (tp - price) <= 0:
        return None
    return Signal(symbol, side, ts, price, stop, tp, reason)


def efficiency_ratio(c: Sequence[float], n: int) -> float:
    """Kaufman efficiency ratio over n bars: |net move| / sum of |moves|. ~1 = clean trend, ~0 = chop."""
    if len(c) < n + 1:
        return 0.0
    w = c[-n - 1:]
    path = sum(abs(w[i] - w[i - 1]) for i in range(1, len(w)))
    return abs(w[-1] - w[0]) / path if path > 0 else 0.0


def chandelier_stop(st, pos, mult: float, atr_n: int = 14) -> float | None:
    """Trailing stop: best structure-TF close since entry minus `mult` × ATR (mirrored for shorts)."""
    t, c, h, lo = list(st.t_s), list(st.c_s), list(st.h_s), list(st.l_s)
    since = [x for ts, x in zip(t, c) if ts >= pos.entry_ts]
    if not since or len(c) < atr_n + 1:
        return None
    a = atr(h, lo, c, atr_n)
    if pos.side.sign > 0:
        return max(since) - mult * a
    return min(since) + mult * a
