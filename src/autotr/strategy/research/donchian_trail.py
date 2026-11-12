"""Donchian breakout with a trailing stop (round 3, turtle-style): break of the prior N-bar
high/low in the direction of the EMA; initial stop `stop_atr` ATR, then a chandelier trail; no
fixed target. Run on 4h bars to cut the noise trades of the 1h version (≈350 trades/quarter)."""

from __future__ import annotations

from autotr.strategy.research._common import chandelier_stop
from autotr.strategy.research.donchian_trend import Strategy as _Base

DEFAULTS = dict(lookback=30, ema=50, stop_atr=2.5, tp_r=50.0, ema_filter=True, trail_atr=3.0)


class Strategy(_Base):
    def __init__(self, cfg, fees):
        super().__init__(cfg, fees)
        self.p = {**DEFAULTS, **cfg.params}

    def manage(self, st, pos):
        return chandelier_stop(st, pos, self.p["trail_atr"])
