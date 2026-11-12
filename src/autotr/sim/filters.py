"""Binance order filters (tick size, lot step, minimum notional).

Real values come from `GET /fapi/v1/exchangeInfo`, cached to `{data_dir}/exchange_info.json`
(`autotr fetch-exchange-info`, or automatically in live mode). When that file is absent — e.g.
in an environment that cannot reach the Binance API — conservative defaults derived from the
price are used, plus a few known values for the majors.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SymbolFilters:
    tick_size: float
    step_size: float
    min_qty: float
    min_notional: float

    def round_qty(self, qty: float) -> float:
        """Round quantity *down* to the lot step."""
        step = Decimal(str(self.step_size))
        return float((Decimal(str(qty)) / step).to_integral_value(ROUND_DOWN) * step)

    def round_price(self, price: float) -> float:
        tick = Decimal(str(self.tick_size))
        return float((Decimal(str(price)) / tick).to_integral_value(ROUND_HALF_UP) * tick)


# Approximate values for majors whose minimums differ from the $5 default.
KNOWN_FILTERS: dict[str, SymbolFilters] = {
    "BTCUSDT": SymbolFilters(0.1, 0.001, 0.001, 100.0),
    "ETHUSDT": SymbolFilters(0.01, 0.001, 0.001, 20.0),
}


def default_filters(price: float) -> SymbolFilters:
    mag = math.floor(math.log10(price)) if price > 0 else 0
    tick = 10.0 ** (mag - 4)          # ~5 significant digits of price precision
    step = 10.0 ** math.floor(math.log10(0.5 / price)) if price > 0 else 1.0  # step worth < $0.50
    step = max(step, 1e-8)
    return SymbolFilters(tick, step, step, 5.0)


class FilterBook:
    def __init__(self, filters: dict[str, SymbolFilters] | None = None):
        self._filters: dict[str, SymbolFilters] = dict(filters or {})
        self._defaults: dict[str, SymbolFilters] = {}

    def get(self, symbol: str, price: float) -> SymbolFilters:
        f = self._filters.get(symbol) or KNOWN_FILTERS.get(symbol)
        if f is not None:
            return f
        if symbol not in self._defaults:
            self._defaults[symbol] = default_filters(price)
        return self._defaults[symbol]

    @property
    def has_real_data(self) -> bool:
        return bool(self._filters)

    # ------------------------------------------------------------ exchangeInfo
    @staticmethod
    def parse_exchange_info(info: dict) -> dict[str, SymbolFilters]:
        out: dict[str, SymbolFilters] = {}
        for s in info.get("symbols", []):
            fl = {f["filterType"]: f for f in s.get("filters", [])}
            try:
                out[s["symbol"]] = SymbolFilters(
                    tick_size=float(fl["PRICE_FILTER"]["tickSize"]),
                    step_size=float(fl["MARKET_LOT_SIZE"]["stepSize"] if "MARKET_LOT_SIZE" in fl else fl["LOT_SIZE"]["stepSize"]),
                    min_qty=float(fl["MARKET_LOT_SIZE"]["minQty"] if "MARKET_LOT_SIZE" in fl else fl["LOT_SIZE"]["minQty"]),
                    min_notional=float(fl.get("MIN_NOTIONAL", {}).get("notional", 5.0)),
                )
            except (KeyError, ValueError):
                continue
        return out

    @classmethod
    def load(cls, data_dir: str | Path) -> "FilterBook":
        path = Path(data_dir) / "exchange_info.json"
        if not path.exists():
            return cls()
        return cls(cls.parse_exchange_info(json.loads(path.read_text())))
