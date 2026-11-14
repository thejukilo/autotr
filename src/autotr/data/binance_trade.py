"""Signed Binance USDT-M futures *trade* client — places REAL orders. Synchronous (httpx.Client).

This is the only part of autotr that can move real money. It is deliberately separate from the
public market-data client (`data/rest.py`): every request here is HMAC-SHA256 signed with your API
secret, and the key/secret are read from the ENVIRONMENT, never from config files or the repo.

    BINANCE_API_KEY / BINANCE_API_SECRET                 — real account (mainnet)
    BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET — testnet.binancefuture.com

Defaults to the Futures TESTNET. Real trading requires `testnet=False` to be passed explicitly.

Synchronous on purpose: orders are infrequent (minutes apart) and must be confirmed before the
engine proceeds, so a blocking call inside the async event loop is simpler and safer than threading
coroutines through the exchange interface.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
import urllib.parse

import httpx

log = logging.getLogger("autotr.trade")

MAINNET = "https://fapi.binance.com"
TESTNET = "https://testnet.binancefuture.com"


class BinanceError(RuntimeError):
    """A non-2xx response from Binance, carrying its error `code` (e.g. -2019 insufficient margin)."""

    def __init__(self, status: int, code: int | None, msg: str | None):
        super().__init__(f"HTTP {status} / code {code}: {msg}")
        self.status = status
        self.code = code
        self.msg = msg


def sign(secret: bytes, query: str) -> str:
    """HMAC-SHA256 of the query string, hex-encoded — exactly Binance's scheme."""
    return hmac.new(secret, query.encode(), hashlib.sha256).hexdigest()


class BinanceTrade:
    def __init__(self, key: str | None, secret: str | None, *, testnet: bool = True,
                 recv_window: int = 5000, timeout: float = 15.0,
                 transport: httpx.BaseTransport | None = None):
        if not key or not secret:
            raise RuntimeError(
                "Binance API key/secret are missing. Set them in the environment before running "
                "live-real:\n"
                "  BINANCE_API_KEY / BINANCE_API_SECRET            (real account)\n"
                "  BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET (testnet)\n"
                "They must never go in a config file or the repo.")
        self.key = key
        self._secret = secret.encode()
        self.testnet = testnet
        self.base = TESTNET if testnet else MAINNET
        self.recv_window = recv_window
        self.client = httpx.Client(base_url=self.base, timeout=timeout,
                                   headers={"X-MBX-APIKEY": key}, transport=transport)

    @classmethod
    def from_env(cls, *, testnet: bool = True, **kw) -> "BinanceTrade":
        if testnet:
            key = os.environ.get("BINANCE_TESTNET_API_KEY") or os.environ.get("BINANCE_API_KEY")
            secret = os.environ.get("BINANCE_TESTNET_API_SECRET") or os.environ.get("BINANCE_API_SECRET")
        else:
            key = os.environ.get("BINANCE_API_KEY")
            secret = os.environ.get("BINANCE_API_SECRET")
        return cls(key, secret, testnet=testnet, **kw)

    def close(self) -> None:
        self.client.close()

    # ------------------------------------------------------------ transport
    def _sign_query(self, params: dict) -> str:
        query = urllib.parse.urlencode(params, doseq=True)
        return query + "&signature=" + sign(self._secret, query)

    def _parse(self, r: httpx.Response):
        try:
            data = r.json()
        except Exception:
            data = None
        if r.status_code >= 400:
            code = data.get("code") if isinstance(data, dict) else None
            msg = data.get("msg") if isinstance(data, dict) else (r.text or "")
            raise BinanceError(r.status_code, code, msg)
        return data

    def _signed(self, method: str, path: str, **params):
        params = {k: v for k, v in params.items() if v is not None}
        params["timestamp"] = int(time.time() * 1000)
        params.setdefault("recvWindow", self.recv_window)
        return self._parse(self.client.request(method, path + "?" + self._sign_query(params)))

    # ------------------------------------------------------------ account / config
    def wallet_usdt(self) -> tuple[float, float]:
        """(total wallet balance, available balance) in USDT on the futures account."""
        for a in self._signed("GET", "/fapi/v2/balance"):
            if a["asset"] == "USDT":
                return float(a["balance"]), float(a["availableBalance"])
        return 0.0, 0.0

    def position_risk(self, symbol: str | None = None) -> list[dict]:
        return self._signed("GET", "/fapi/v2/positionRisk", symbol=symbol)

    def open_orders(self, symbol: str | None = None) -> list[dict]:
        return self._signed("GET", "/fapi/v1/openOrders", symbol=symbol)

    def order_status(self, symbol: str, order_id) -> dict:
        return self._signed("GET", "/fapi/v1/order", symbol=symbol, orderId=order_id)

    def set_one_way_mode(self) -> None:
        """Ensure one-way (not hedge) position mode; our reduce-only/closePosition logic assumes it."""
        try:
            self._signed("POST", "/fapi/v1/positionSide/dual", dualSidePosition="false")
        except BinanceError as e:
            if e.code != -4059:  # -4059 = "No need to change position side."
                raise

    def set_leverage(self, symbol: str, leverage: int) -> dict:
        return self._signed("POST", "/fapi/v1/leverage", symbol=symbol, leverage=int(leverage))

    def set_isolated(self, symbol: str) -> None:
        try:
            self._signed("POST", "/fapi/v1/marginType", symbol=symbol, marginType="ISOLATED")
        except BinanceError as e:
            if e.code != -4046:  # -4046 = "No need to change margin type."
                raise

    # ------------------------------------------------------------ orders
    def market(self, symbol: str, side: str, qty, *, reduce_only: bool = False) -> dict:
        return self._signed("POST", "/fapi/v1/order", symbol=symbol, side=side, type="MARKET",
                            quantity=qty, reduceOnly="true" if reduce_only else None,
                            newOrderRespType="RESULT")

    def limit(self, symbol: str, side: str, qty, price, *, post_only: bool = True) -> dict:
        return self._signed("POST", "/fapi/v1/order", symbol=symbol, side=side, type="LIMIT",
                            timeInForce="GTX" if post_only else "GTC", quantity=qty, price=price,
                            newOrderRespType="RESULT")

    def stop_market(self, symbol: str, side: str, stop_price) -> dict:
        """Reduce-only STOP_MARKET that closes the whole position when the mark price hits it."""
        return self._signed("POST", "/fapi/v1/order", symbol=symbol, side=side, type="STOP_MARKET",
                            stopPrice=stop_price, closePosition="true", workingType="MARK_PRICE",
                            priceProtect="true")

    def take_profit_market(self, symbol: str, side: str, stop_price) -> dict:
        return self._signed("POST", "/fapi/v1/order", symbol=symbol, side=side,
                            type="TAKE_PROFIT_MARKET", stopPrice=stop_price, closePosition="true",
                            workingType="MARK_PRICE", priceProtect="true")

    def cancel(self, symbol: str, order_id) -> dict:
        return self._signed("DELETE", "/fapi/v1/order", symbol=symbol, orderId=order_id)

    def cancel_all(self, symbol: str) -> dict:
        return self._signed("DELETE", "/fapi/v1/allOpenOrders", symbol=symbol)

    def order_test(self, symbol: str, side: str, qty) -> dict:
        """Validate order params against Binance WITHOUT sending to the matching engine."""
        return self._signed("POST", "/fapi/v1/order/test", symbol=symbol, side=side, type="MARKET",
                            quantity=qty)
