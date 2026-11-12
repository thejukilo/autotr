"""Volatility scanner: choose which symbols the strategy is allowed to trade right now."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from autotr.config import ScannerCfg
from autotr.market.features import SymbolState
from autotr.models import MINUTE_MS

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ScanRow:
    symbol: str
    volatility: float      # mean 5m true range / price
    quote_volume_24h: float


class Scanner:
    def __init__(self, cfg: ScannerCfg):
        self.cfg = cfg
        self.active: set[str] = set()
        self.last_scan_ts: int | None = None
        self.last_rows: list[ScanRow] = []

    def due(self, ts: int) -> bool:
        return self.last_scan_ts is None or ts - self.last_scan_ts >= self.cfg.rescan_minutes * MINUTE_MS

    def scan(self, states: dict[str, SymbolState], ts: int,
             volume_24h: dict[str, float] | None = None) -> list[ScanRow]:
        """Rank symbols by volatility among those with enough 24h quote volume.

        `volume_24h` (live: from the exchange's 24h ticker) overrides the rolling volume computed
        from bars, which needs a full day of history (backtest warm-up).
        """
        rows: list[ScanRow] = []
        for sym, st in states.items():
            vol = st.volatility
            if vol is None or not st.ready:
                continue
            if volume_24h is not None:
                qv = volume_24h.get(sym, 0.0)
            elif st.has_24h_history:
                qv = st.quote_volume_24h
            else:
                continue
            if qv < self.cfg.min_quote_volume_24h:
                continue
            rows.append(ScanRow(sym, vol, qv))
        rows.sort(key=lambda r: r.volatility, reverse=True)
        top = rows[: self.cfg.top_n]
        new_active = {r.symbol for r in top}
        if new_active != self.active:
            log.debug("scanner %s: %s", ts, ", ".join(f"{r.symbol}({r.volatility:.3%})" for r in top))
        self.active = new_active
        self.last_scan_ts = ts
        self.last_rows = rows
        return top
