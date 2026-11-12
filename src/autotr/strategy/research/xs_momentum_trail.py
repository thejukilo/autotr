"""Cross-sectional momentum with a trailing stop (round 3). Same entries as xs_momentum; no fixed
target by default — a chandelier stop (best close since entry − `trail_atr` × ATR) lets winners run.
Tester-B finding: xs_momentum had a gross edge but its profit came from a handful of trades that
hit 3R or the time stop; capping them at 3R wastes the right tail."""

from __future__ import annotations

from autotr.strategy.research._common import chandelier_stop
from autotr.strategy.research.xs_momentum import Strategy as _Base

DEFAULTS = dict(lookback=42, k=3, ema=50, stop_atr=3.0, tp_r=50.0, trail_atr=3.0, min_spread=0.0, min_coins=10, longs=True, shorts=True)


class Strategy(_Base):
    def __init__(self, cfg, fees):
        super().__init__(cfg, fees)
        self.p = {**DEFAULTS, **cfg.params}

    def on_bar(self, st):
        if self.p["min_spread"] > 0 and len(self.mom) >= 2:
            v = sorted(self.mom.values())
            if v[-1] - v[0] < self.p["min_spread"]:
                return None
        return super().on_bar(st)

    def manage(self, st, pos):
        return chandelier_stop(st, pos, self.p["trail_atr"])
