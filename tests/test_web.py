"""Web dashboard: snapshot from a live SQLite account, and the password-protected HTTP server."""

import base64
import json
import threading
import urllib.error
import urllib.request

from autotr.config import load_config
from autotr.models import MINUTE_MS, EquityPoint, Side, Trade
from autotr.report.tradelog import LiveStore
from autotr.web import load_password, make_server, snapshot

NOW = 1_794_500_000_000 - 1_794_500_000_000 % MINUTE_MS


def _account(path):
    store = LiveStore(path)
    store.add_trade(Trade("AAAUSDT", Side.LONG, 1.0, 100.0, 103.0, NOW - 90 * MINUTE_MS, NOW - 30 * MINUTE_MS,
                          3.0, 0.1, 0.0, "trail", 1.5))
    for i in range(5):
        store.add_equity(EquityPoint(NOW - (5 - i) * 5 * MINUTE_MS, 100 + i, 100 + i, 0))
    store.save_state("engine", {
        "exchange": {
            "balance": 102.9,
            "positions": [{"symbol": "BBBUSDT", "side": "SHORT", "qty": 2.0, "entry_price": 50.0, "entry_ts": NOW,
                           "stop": 52.0, "take_profit": 0.0, "margin": 20.0, "liq_price": 60.0, "entry_fee": 0.05,
                           "risk_usd": 4.0, "time_stop_ts": 0, "funding": 0.0}],
            "pending": [], "stats": {}, "last_price": {"BBBUSDT": 49.0},
        },
        "risk": {}, "last_minute": NOW - MINUTE_MS,
    })
    store.close()


def test_snapshot(tmp_path):
    db = tmp_path / "live.db"
    cfg = load_config()
    assert snapshot(db, cfg, NOW)["account"] is None  # no file yet
    _account(db)
    s = snapshot(db, cfg, NOW)
    json.dumps(s, allow_nan=False)  # browsers reject NaN / Infinity
    (p,) = s["positions"]
    assert p["upnl"] == 2.0  # short 2 @ 50, now 49
    assert s["account"]["equity"] == 104.9 and s["running"]
    assert s["metrics"]["trades"] == 1 and s["trades"][0]["symbol"] == "AAAUSDT"
    assert s["equity"][-1] == [NOW - MINUTE_MS, 104.9]  # live point appended after the snapshots
    assert s["next_decision"] > NOW and s["next_decision"] % (cfg.strategy.entry_tf_minutes * MINUTE_MS) == 0
    assert not snapshot(db, cfg, NOW + 10 * MINUTE_MS)["running"]  # no candle for 11 min


def test_password_file(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTOTR_WEB_PASSWORD", raising=False)
    pw, path = load_password(tmp_path)
    assert len(pw) >= 12 and path.read_text().strip() == pw and (path.stat().st_mode & 0o077) == 0
    assert load_password(tmp_path) == (pw, path)  # stable across restarts
    monkeypatch.setenv("AUTOTR_WEB_PASSWORD", "from-env")
    assert load_password(tmp_path) == ("from-env", None)


def test_server_requires_password(tmp_path):
    db = tmp_path / "live.db"
    _account(db)
    srv = make_server("127.0.0.1", 0, db, load_config(), "s3cret")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def get(path, pw=None):
        req = urllib.request.Request(base + path)
        if pw is not None:
            req.add_header("Authorization", "Basic " + base64.b64encode(f"me:{pw}".encode()).decode())
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, b""

    try:
        assert get("/api/status")[0] == 401
        assert get("/api/status", "wrong")[0] == 401
        code, body = get("/api/status", "s3cret")
        assert code == 200 and json.loads(body)["positions"][0]["symbol"] == "BBBUSDT"
        code, body = get("/", "s3cret")
        assert code == 200 and b"<title>autotr" in body
    finally:
        srv.shutdown()
        srv.server_close()
