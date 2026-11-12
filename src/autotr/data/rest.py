"""Minimal async client for Binance USDT-M futures public REST endpoints (no API key needed)."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import httpx

from autotr.models import Bar

log = logging.getLogger(__name__)


class BinanceRest:
    def __init__(self, base_url: str = "https://fapi.binance.com", timeout: float = 15.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.client = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport)

    async def close(self) -> None:
        await self.client.aclose()

    async def _get(self, path: str, **params):
        r = await self.client.get(path, params=params or None)
        if r.status_code == 451:
            raise RuntimeError(
                "Binance refused the request (HTTP 451: restricted location). The live feed must run "
                "from a location where Binance futures is available.")
        r.raise_for_status()
        return r.json()

    async def exchange_info(self) -> dict:
        return await self._get("/fapi/v1/exchangeInfo")

    async def ticker_24h(self) -> dict[str, float]:
        """24h quote volume per symbol."""
        data = await self._get("/fapi/v1/ticker/24hr")
        return {d["symbol"]: float(d["quoteVolume"]) for d in data}

    async def klines(self, symbol: str, limit: int = 1500, start_time: int | None = None) -> list[Bar]:
        """Closed 1m bars (the still-forming last bar is dropped)."""
        params: dict = {"symbol": symbol, "interval": "1m", "limit": min(limit, 1500)}
        if start_time is not None:
            params["startTime"] = start_time
        rows = await self._get("/fapi/v1/klines", **params)
        now_ms = int(time.time() * 1000)
        return [Bar(symbol, int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                    float(r[5]), float(r[7]))
                for r in rows if int(r[6]) < now_ms]  # r[6] = close time; drop the forming bar


def usdt_perpetuals(info: dict) -> list[str]:
    return [s["symbol"] for s in info.get("symbols", [])
            if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT"
            and s.get("status") == "TRADING"]


def save_exchange_info(info: dict, data_dir: str | Path) -> Path:
    path = Path(data_dir) / "exchange_info.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(info))
    return path

