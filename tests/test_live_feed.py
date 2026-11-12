"""BinanceFeed against a local fake: WebSocket server speaking Binance's combined-stream format and a
mocked REST API. Verifies parsing, symbol selection, warm-up, funding detection and the
connect → stream loop."""

import asyncio
import json
import time

import httpx
import websockets

from autotr.config import load_config
from autotr.data.events import Funding, KlineClosed, Quote, WarmupBars
from autotr.data.live import BinanceFeed
from autotr.data.rest import BinanceRest
from autotr.models import MINUTE_MS

SYM = "AAAUSDT"


def _now_min() -> int:
    t = int(time.time() * 1000)
    return t - t % MINUTE_MS


def fake_rest() -> BinanceRest:
    base = _now_min() - 1500 * MINUTE_MS

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/fapi/v1/exchangeInfo":
            return httpx.Response(200, json={"symbols": [
                {"symbol": SYM, "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING", "filters": []},
                {"symbol": "OLDUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "SETTLING", "filters": []},
            ]})
        if path == "/fapi/v1/ticker/24hr":
            return httpx.Response(200, json=[{"symbol": SYM, "quoteVolume": "123000000"},
                                             {"symbol": "OLDUSDT", "quoteVolume": "999999999"}])
        if path == "/fapi/v1/klines":
            rows = []
            for i in range(1500):
                t = base + i * MINUTE_MS
                rows.append([t, "100", "100.5", "99.5", "100", "10", t + MINUTE_MS - 1, "1000", 5, "5", "500", "0"])
            return httpx.Response(200, json=rows)
        return httpx.Response(404)

    return BinanceRest("https://fapi.test", transport=httpx.MockTransport(handler))


def test_parse_messages():
    cfg = load_config()
    feed = BinanceFeed(cfg, fake_rest())
    feed._symbols = [SYM]
    k = {"stream": "aaausdt@kline_1m", "data": {"e": "kline", "E": 1, "s": SYM, "k": {
        "t": 60000, "T": 119999, "s": SYM, "i": "1m", "o": "1", "c": "2", "h": "3", "l": "0.5",
        "v": "10", "q": "15", "x": False}}}
    assert feed._parse(k) == []  # candle not closed yet
    k["data"]["k"]["x"] = True
    (ev,) = feed._parse(k)
    assert isinstance(ev, KlineClosed) and ev.bar.close == 2 and ev.bar.quote_volume == 15
    assert feed._parse(k) == []  # duplicate ignored
    (q,) = feed._parse({"stream": "aaausdt@bookTicker", "data": {"e": "bookTicker", "s": SYM, "b": "1.5",
                                                                  "B": "3", "a": "1.6", "A": "2", "T": 5, "E": 6}})
    assert isinstance(q, Quote) and (q.bid, q.ask, q.ts) == (1.5, 1.6, 5)
    mp = lambda T, r, p: {"stream": "!markPrice@arr", "data": [{"e": "markPriceUpdate", "s": SYM, "p": p, "r": r, "T": T},
                                                               {"e": "markPriceUpdate", "s": "ZZZUSDT", "p": "1", "r": "0.1", "T": T}]}
    assert feed._parse(mp(1000, "0.0001", "100")) == []
    assert feed._parse(mp(1000, "0.0002", "101")) == []
    (f,) = feed._parse(mp(2000, "0.0003", "102"))  # next funding time advanced -> settled at 1000
    assert isinstance(f, Funding) and (f.rate, f.mark_price, f.ts) == (0.0002, 101.0, 1000)


def test_feed_end_to_end_with_fake_server():
    asyncio.run(_e2e())


async def _e2e():
    t0 = _now_min()

    paths = []

    async def server(ws):
        # Like Binance: klines + mark prices only on /market, quotes only on /public.
        path = ws.request.path
        paths.append(path)
        if path.startswith("/market/stream?"):
            assert "aaausdt@kline_1m" in path and "!markPrice@arr" in path and "bookTicker" not in path
            await asyncio.sleep(0.2)  # let the quote arrive first
            msgs = [
                {"stream": "aaausdt@kline_1m", "data": {"e": "kline", "s": SYM, "k": {
                    "t": t0, "T": t0 + MINUTE_MS - 1, "s": SYM, "o": "100", "c": "100.2", "h": "100.4", "l": "99.9",
                    "v": "5", "q": "500", "x": True}}},
                {"stream": "!markPrice@arr", "data": [{"s": SYM, "p": "100", "r": "0.0001", "T": t0 + 10}]},
            ]
        elif path.startswith("/public/stream?"):
            assert "aaausdt@bookTicker" in path and "kline" not in path
            msgs = [{"stream": "aaausdt@bookTicker", "data": {"e": "bookTicker", "s": SYM, "b": "100.0", "a": "100.1", "T": t0 + 1}}]
        else:
            raise AssertionError(f"unexpected path {path}")
        for m in msgs:
            await ws.send(json.dumps(m))
        await asyncio.sleep(5)

    async with websockets.serve(server, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        cfg = load_config()
        cfg.live.ws_url = f"ws://127.0.0.1:{port}"
        cfg.universe.live_candidates = 5
        feed = BinanceFeed(cfg, fake_rest())
        got = []
        async def collect():
            async for ev in feed.events():
                got.append(ev)
                if isinstance(ev, KlineClosed) and not ev.backfill:
                    return
        await asyncio.wait_for(collect(), 20)
        await feed.rest.close()

    kinds = [type(e).__name__ for e in got]
    assert kinds[0] == "Volumes24h" and got[0].quote_volume[SYM] == 123e6
    warm = [e for e in got if isinstance(e, WarmupBars)]
    assert len(warm) == 1 and warm[0].bars[0].symbol == SYM and len(warm[0].bars) >= 1400
    assert any(isinstance(e, Quote) for e in got)
    assert isinstance(got[-1], KlineClosed) and got[-1].bar.close == 100.2
    assert feed._symbols == [SYM]  # non-TRADING symbol filtered out
    assert sorted(p.split("/")[1] for p in paths) == ["market", "public"]
