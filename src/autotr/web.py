"""Read-only web dashboard for the live paper account (`autotr web`).

Serves one page plus `/api/status` (JSON) from the same SQLite file the live bot writes, opened
read-only, so it can never disturb the bot. Protected with HTTP basic auth: the password comes from
$AUTOTR_WEB_PASSWORD or `<data dir>/web_password.txt` (generated on first start). Standard library
only.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import math
import os
import secrets
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from autotr.config import Config
from autotr.models import MINUTE_MS, EquityPoint, Side, Trade
from autotr.report.metrics import compute_metrics

log = logging.getLogger(__name__)

STALE_MS = 5 * MINUTE_MS      # no new candle for this long -> bot shown as not running
MAX_CHART_POINTS = 600
MAX_TRADES = 200


# ------------------------------------------------------------------ data
def _read(db_path: Path) -> tuple[list[Trade], list[EquityPoint], dict | None]:
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        rows = db.execute(
            "SELECT symbol, side, qty, entry_price, exit_price, entry_ts, exit_ts, gross_pnl, fees, funding,"
            " exit_reason, risk_usd, entry_reason, COALESCE(stop, 0), COALESCE(take_profit, 0),"
            " COALESCE(fills, 1), COALESCE(confidence, 1.0) FROM trades ORDER BY id").fetchall()
        trades = [Trade(r[0], Side(r[1]), *r[2:16], confidence=r[16]) for r in rows]
        equity = [EquityPoint(*r) for r in db.execute(
            "SELECT ts, equity, balance, open_positions FROM equity ORDER BY ts")]
        row = db.execute("SELECT value FROM state WHERE key = 'engine'").fetchone()
        return trades, equity, (json.loads(row[0]) if row else None)
    finally:
        db.close()


def _finite(x):
    if isinstance(x, float) and not math.isfinite(x):
        return None
    if isinstance(x, dict):
        return {k: _finite(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_finite(v) for v in x]
    return x


def snapshot(db_path: Path, cfg: Config, now_ms: int | None = None) -> dict:
    """Everything the dashboard shows, as plain JSON-able data."""
    now_ms = now_ms or int(time.time() * 1000)
    cad_s = cfg.live.decision_seconds or cfg.strategy.entry_tf_minutes * 60  # seconds between decisions
    tf_ms = max(int(cad_s * 1000), 1000)
    base = {
        "now": now_ms,
        "strategy": cfg.strategy.name,
        "account_file": db_path.name,
        "next_decision": (now_ms // tf_ms + 1) * tf_ms,
        "decision_seconds": cad_s,
        "confidence_sizing": cfg.risk.confidence_sizing,
    }
    if not db_path.exists():
        return {**base, "account": None}
    trades, equity, state = _read(db_path)
    if state is None:
        return {**base, "account": None}

    ex = state["exchange"]
    last_price = ex.get("last_price", {})
    positions = []
    for p in ex["positions"]:
        last = last_price.get(p["symbol"], p["entry_price"])
        sign = 1 if p["side"] == Side.LONG.value else -1
        upnl = sign * (last - p["entry_price"]) * p["qty"]
        rungs = tuple(p.get("rungs", ()))
        positions.append({
            "symbol": p["symbol"], "side": p["side"], "qty": p["qty"], "entry_price": p["entry_price"],
            "last_price": last, "stop": p["stop"], "take_profit": p["take_profit"], "entry_ts": p["entry_ts"],
            "margin": p["margin"], "risk_usd": p["risk_usd"], "upnl": upnl,
            "upnl_r": upnl / p["risk_usd"] if p.get("risk_usd") else 0.0,
            "conf": p.get("confidence", 1.0), "fills": p.get("fills", 1), "planned_rungs": len(rungs),
            "trailed": bool(p.get("stop_trailed")), "time_stop_ts": p.get("time_stop_ts"),
        })
    # Pending entries: un-triggered limit-order ladders (and their fill progress).
    pending = []
    for pe in ex.get("pending", []):
        last = last_price.get(pe["symbol"], 0.0)
        sign = 1 if pe["side"] == Side.LONG.value else -1
        rungs = [{
            "price": r["price"], "qty": r["qty"], "filled": bool(r["filled"]), "market": r["price"] is None,
            "dist_pct": ((r["price"] / last - 1) * 100 * sign) if (r["price"] and last) else None,
        } for r in pe.get("rungs", [])]
        pending.append({
            "symbol": pe["symbol"], "side": pe["side"], "stop": pe["stop"], "take_profit": pe["take_profit"],
            "conf": pe.get("confidence", 1.0), "created_ts": pe["created_ts"], "expiry_ts": pe.get("expiry_ts"),
            "last_price": last, "rungs": rungs,
            "filled": sum(1 for r in rungs if r["filled"]), "total": len(rungs),
        })
    balance = ex["balance"]
    upnl = sum(p["upnl"] for p in positions)
    start_bal = cfg.account.starting_balance
    last_minute = state.get("last_minute")

    # Same reconciliation as `autotr status`: open positions already paid entry fee / funding.
    realised = balance + sum(p["entry_fee"] - p["funding"] for p in ex["positions"])
    start_ts = equity[0].ts if equity else (trades[0].entry_ts if trades else now_ms)
    m = compute_metrics(trades, equity, start_bal, realised, start_ts, max(now_ms, start_ts + 1))

    curve = [(e.ts, e.equity) for e in equity]
    if last_minute and (not curve or last_minute > curve[-1][0]):
        curve.append((last_minute, balance + upnl))
    step = max(1, math.ceil(len(curve) / MAX_CHART_POINTS))
    sampled = curve[::step]
    if curve and sampled[-1] != curve[-1]:
        sampled.append(curve[-1])

    recent = [{
        "symbol": t.symbol, "side": t.side.value, "entry_ts": t.entry_ts, "exit_ts": t.exit_ts,
        "entry_price": t.entry_price, "exit_price": t.exit_price, "net_pnl": t.net_pnl,
        "r": t.r_multiple, "fees": t.fees, "reason": t.exit_reason,
        "conf": t.confidence, "risk_usd": t.risk_usd,
    } for t in reversed(trades[-MAX_TRADES:])]

    return _finite({
        **base,
        "account": {
            "starting_balance": start_bal,
            "balance": balance,
            "equity": balance + upnl,
            "unrealized": upnl,
            "return_pct": (balance + upnl) / start_bal * 100 - 100,
            "since": start_ts,
        },
        "last_update": last_minute,
        "running": bool(last_minute) and now_ms - last_minute < STALE_MS,
        "pending_entries": len(pending),
        "metrics": {k: m[k] for k in ("trades", "win_rate", "net_pnl", "fees", "funding", "profit_factor",
                                      "expectancy_r", "max_drawdown_pct", "by_side", "per_symbol")},
        "positions": positions,
        "pending": pending,
        "trades": recent,
        "equity": [[ts, round(v, 4)] for ts, v in sampled],
    })


# ------------------------------------------------------------------ auth
def load_password(data_dir: Path) -> tuple[str, Path | None]:
    """$AUTOTR_WEB_PASSWORD, else <data dir>/web_password.txt (created with a random password)."""
    env = os.environ.get("AUTOTR_WEB_PASSWORD", "").strip()
    if env:
        return env, None
    path = data_dir / "web_password.txt"
    if path.exists() and path.read_text().strip():
        return path.read_text().strip(), path
    path.parent.mkdir(parents=True, exist_ok=True)
    pw = secrets.token_urlsafe(12)
    path.write_text(pw + "\n")
    path.chmod(0o600)
    return pw, path


def _authorized(header: str | None, password: str) -> bool:
    if not header or not header.startswith("Basic "):
        return False
    try:
        _, _, given = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except Exception:  # noqa: BLE001
        return False
    return hmac.compare_digest(given.encode(), password.encode())


# ------------------------------------------------------------------ server
def make_server(host: str, port: int, db_path: Path, cfg: Config, password: str) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "autotr"
        sys_version = ""

        def log_message(self, fmt, *args):  # quiet: the bot's console is busy enough
            pass

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if not _authorized(self.headers.get("Authorization"), password):
                log.warning("dashboard: refused login from %s", self.client_address[0])
                time.sleep(1)  # slow down password guessing
                self._send(401, b"login required", "text/plain; charset=utf-8",
                           {"WWW-Authenticate": 'Basic realm="autotr", charset="UTF-8"'})
                return
            path = self.path.split("?", 1)[0]
            if path == "/":
                sec = cfg.live.ui_refresh_seconds
                page = PAGE.replace("__REFRESH_MS__", str(int(sec * 1000))).replace("__REFRESH__", "%g" % sec)
                self._send(200, page.encode(), "text/html; charset=utf-8")
            elif path == "/api/status":
                try:
                    data = snapshot(db_path, cfg)
                except Exception as exc:  # noqa: BLE001
                    log.exception("dashboard: snapshot failed")
                    data = {"error": str(exc)}
                self._send(200, json.dumps(data).encode(), "application/json")
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")

    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    return srv


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>autotr — paper account</title>
<style>
:root {
  color-scheme: light;
  --surface-0: #f4f4f2; --surface-1: #fcfcfb; --surface-2: #ececea; --border: #e3e2de;
  --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #7a7974;
  --grid: #ecebe7; --series-1: #2a78d6;
  --good: #0ca30c; --good-text: #047804; --good-bg: #e6f6e6;
  --critical: #d03b3b; --critical-text: #b42f2f; --critical-bg: #fbe7e7;
  --info: #2a78d6; --info-text: #1f66bd; --info-bg: #e6effb;
  --warn: #c98a00; --warn-text: #9c6c00; --warn-bg: #faf0d8;
  --shadow: 0 1px 2px rgba(0,0,0,.04), 0 6px 20px rgba(0,0,0,.05);
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --surface-0: #111110; --surface-1: #1a1a19; --surface-2: #232321; --border: #2c2c2a;
    --text-primary: #f0efec; --text-secondary: #c3c2b7; --text-muted: #8f8e86;
    --grid: #2a2a28; --series-1: #3987e5;
    --good-text: #51cc51; --good-bg: #162d16;
    --critical-text: #ef7a7a; --critical-bg: #391c1c;
    --info-text: #6fa8e8; --info-bg: #182a40; --warn-text: #d8a93a; --warn-bg: #332a14;
    --shadow: 0 1px 2px rgba(0,0,0,.3), 0 6px 20px rgba(0,0,0,.35);
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--surface-0); color: var(--text-primary);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { max-width: 1120px; margin: 0 auto; padding: 18px 16px 48px; }
header { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 14px; margin-bottom: 16px; }
h1 { font-size: 18px; margin: 0; font-weight: 700; letter-spacing: -.01em; }
h2 { font-size: 13px; margin: 0; font-weight: 600; text-transform: uppercase; letter-spacing: .04em; color: var(--text-secondary); }
.spacer { flex: 1; }
.sub { color: var(--text-secondary); }
.pill { display: inline-flex; align-items: center; gap: 6px; padding: 3px 11px; border-radius: 999px;
  border: 1px solid var(--border); background: var(--surface-1); font-size: 13px; font-weight: 500; }
.dot { width: 9px; height: 9px; border-radius: 50%; background: var(--text-muted); }
.dot.live { background: var(--good); animation: pulse 1.8s ease-in-out infinite; }
.dot.down { background: var(--critical); }
@keyframes pulse { 0%,100% { box-shadow: 0 0 0 0 rgba(12,163,12,.5); } 50% { box-shadow: 0 0 0 5px rgba(12,163,12,0); } }
.btn { font: inherit; font-size: 13px; cursor: pointer; border: 1px solid var(--border); background: var(--surface-1);
  color: var(--text-secondary); border-radius: 8px; padding: 4px 10px; }
.btn:hover { background: var(--surface-2); color: var(--text-primary); }
.card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px;
  margin-bottom: 14px; box-shadow: var(--shadow); }
.card > .head { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.count { color: var(--text-muted); font-size: 12px; font-weight: 500; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(156px, 1fr)); gap: 10px; margin-bottom: 14px; }
.tile { background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px; padding: 12px 14px; box-shadow: var(--shadow); }
.tile .label { color: var(--text-secondary); font-size: 12px; }
.tile .value { font-size: 23px; font-weight: 700; font-variant-numeric: tabular-nums; margin-top: 2px; letter-spacing: -.02em; }
.tile .hint { color: var(--text-muted); font-size: 12px; margin-top: 2px; }
.tile.flash { animation: tileflash 1s ease-out; }
@keyframes tileflash { from { background: var(--info-bg); } to { background: var(--surface-1); } }
.pos { color: var(--good-text); } .neg { color: var(--critical-text); }
.chart-wrap { position: relative; }
svg text { fill: var(--text-muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.tip { position: absolute; pointer-events: none; background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 6px; padding: 6px 8px; font-size: 12px; white-space: nowrap; box-shadow: var(--shadow); display: none; }
.tip b { font-variant-numeric: tabular-nums; }
.scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { padding: 7px 8px; text-align: right; white-space: nowrap; border-bottom: 1px solid var(--border); }
th { color: var(--text-muted); font-weight: 500; font-size: 11px; text-transform: uppercase; letter-spacing: .03em; }
th:first-child, td:first-child, th.l, td.l { text-align: left; }
tbody tr:last-child td { border-bottom: 0; }
tbody tr.flash td { animation: rowflash 1.4s ease-out; }
@keyframes rowflash { 0% { background: var(--info-bg); } 100% { background: transparent; } }
tbody tr.flash-up td { animation: rowup 1.6s ease-out; }
@keyframes rowup { 0% { background: var(--good-bg); } 100% { background: transparent; } }
.side { font-weight: 600; }
.side.long { color: var(--good-text); } .side.short { color: var(--critical-text); }
.badge { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 11px; font-weight: 600;
  border: 1px solid transparent; }
.b-tp { color: var(--good-text); background: var(--good-bg); }
.b-trail { color: var(--info-text); background: var(--info-bg); }
.b-sl { color: var(--critical-text); background: var(--critical-bg); }
.b-time { color: var(--text-secondary); background: var(--surface-2); }
.b-liq { color: #fff; background: var(--critical); }
.conf { display: inline-flex; align-items: center; gap: 6px; }
.confbar { width: 44px; height: 6px; border-radius: 3px; background: var(--surface-2); overflow: hidden; }
.confbar > i { display: block; height: 100%; background: var(--accent, var(--series-1)); }
.cd { font-variant-numeric: tabular-nums; color: var(--text-secondary); }
.lock { color: var(--info-text); font-size: 11px; }
/* pending limit-order ladders */
.ladders { display: grid; grid-template-columns: repeat(auto-fill, minmax(290px, 1fr)); gap: 12px; }
.ladder { border: 1px solid var(--border); border-radius: 10px; padding: 11px 12px; background: var(--surface-0); }
.ladder.flash { animation: rowflash 1.4s ease-out; }
.ladder .lh { display: flex; align-items: center; gap: 8px; margin-bottom: 9px; }
.ladder .lh .sym { font-weight: 700; }
.ladder .lh .meta { margin-left: auto; color: var(--text-muted); font-size: 12px; }
.rung { display: grid; grid-template-columns: 16px 1fr auto; align-items: center; gap: 8px; padding: 3px 0; font-size: 13px; }
.rung .mark { width: 14px; height: 14px; border-radius: 50%; border: 2px solid var(--text-muted); }
.rung.filled .mark { background: var(--good); border-color: var(--good); }
.rung.filled { color: var(--good-text); }
.rung .wgt { color: var(--text-muted); font-size: 11px; }
.rung .px { font-variant-numeric: tabular-nums; text-align: right; }
.rung .dist { color: var(--text-muted); font-size: 11px; }
.lbar { height: 6px; border-radius: 3px; background: var(--surface-2); margin: 8px 0 4px; overflow: hidden; }
.lbar > i { display: block; height: 100%; background: var(--good); transition: width .3s; }
.lfoot { display: flex; gap: 10px; color: var(--text-muted); font-size: 11.5px; flex-wrap: wrap; }
.empty { color: var(--text-muted); padding: 8px 0; }
footer { color: var(--text-muted); font-size: 12px; margin-top: 20px; }
/* toasts */
#toasts { position: fixed; top: 14px; right: 14px; display: flex; flex-direction: column; gap: 8px; z-index: 50; }
.toast { background: var(--surface-1); border: 1px solid var(--border); border-left: 3px solid var(--text-muted);
  border-radius: 8px; padding: 8px 12px; font-size: 13px; box-shadow: var(--shadow); min-width: 180px; max-width: 320px;
  animation: slidein .25s ease-out; }
.toast.out { animation: slideout .3s ease-in forwards; }
.toast b { font-variant-numeric: tabular-nums; }
.toast.tp { border-left-color: var(--good); } .toast.sl { border-left-color: var(--critical); }
.toast.trail { border-left-color: var(--info); } .toast.open { border-left-color: var(--accent, var(--series-1)); }
.toast.fill { border-left-color: var(--warn); }
.toast .k { font-size: 11px; text-transform: uppercase; letter-spacing: .04em; color: var(--text-muted); }
@keyframes slidein { from { opacity: 0; transform: translateX(24px); } to { opacity: 1; transform: none; } }
@keyframes slideout { to { opacity: 0; transform: translateX(24px); height: 0; padding: 0; margin: 0; } }
@media (max-width: 520px) { #toasts { left: 10px; right: 10px; } .toast { max-width: none; } }
</style>
</head>
<body>
<div id="toasts"></div>
<main>
  <header>
    <h1>autotr</h1>
    <span class="sub" id="strategy"></span>
    <span class="pill" id="status"><span class="dot"></span><span>Loading…</span></span>
    <span class="sub" id="updated"></span>
    <span class="spacer"></span>
    <button class="btn" id="pause" title="Pause / resume live updates">⏸ Pause</button>
    <button class="btn" id="refreshNow" title="Refresh now">↻</button>
  </header>
  <div id="error" class="card" style="display:none"></div>
  <div class="tiles" id="tiles"></div>
  <section class="card">
    <div class="head"><h2>Equity</h2></div>
    <div class="chart-wrap" id="chart"></div>
  </section>
  <section class="card" id="pendingCard" style="display:none">
    <div class="head"><h2>Limit orders</h2><span class="count" id="pendingCount"></span></div>
    <div id="pending"></div>
  </section>
  <section class="card">
    <div class="head"><h2>Open positions</h2><span class="count" id="posCount"></span></div>
    <div class="scroll" id="positions"></div>
  </section>
  <section class="card">
    <div class="head"><h2>Closed trades</h2><span class="count" id="tradesCount"></span></div>
    <div class="scroll" id="trades"></div>
  </section>
  <footer>Paper trading only: simulated orders on real Binance prices, no real money. Live updates every __REFRESH__ s.</footer>
</main>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const usd = (v, sign) => (sign && v > 0 ? "+" : v < 0 ? "−" : "") + "$" + Math.abs(v).toFixed(2);
const pct = (v, sign) => (sign && v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(2) + "%";
const cls = v => v > 0 ? "pos" : v < 0 ? "neg" : "";
const price = v => v == null ? "—" : v >= 100 ? v.toFixed(2) : v >= 1 ? v.toFixed(4) : v.toPrecision(4);
const dt = ts => new Date(ts).toLocaleString(undefined, {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"});
const ago = ms => { const s = Math.max(0, Math.round(ms / 1000));
  return s < 90 ? s + " s" : s < 5400 ? Math.round(s / 60) + " min" : s < 172800 ? Math.round(s / 3600) + " h" : Math.round(s / 86400) + " days"; };
const dur = ms => { const s = Math.max(0, Math.ceil(ms / 1000)); return s < 60 ? s + "s" : Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0"); };
const sideHtml = s => s === "LONG" ? `<span class="side long">▲ long</span>` : `<span class="side short">▼ short</span>`;
const confHtml = v => `<span class="conf"><span class="confbar"><i style="width:${Math.round((v || 0) * 100)}%"></i></span>${Math.round((v || 0) * 100)}%</span>`;
const cad = s => s < 60 ? "every " + (s % 1 ? s.toFixed(1) : s) + "s" : s < 3600 ? "every " + Math.round(s / 60) + " min" : "every " + (s / 3600) + " h";
const REASON = {take_profit: ["b-tp", "take profit"], trailing_stop: ["b-trail", "trailing stop"],
  stop_loss: ["b-sl", "stop loss"], time_stop: ["b-time", "time stop"], liquidation: ["b-liq", "liquidation"], manual: ["b-time", "manual"]};
function reasonBadge(r) { const base = String(r).replace(/ \(gap\)$/, ""); const [c, label] = REASON[base] || ["b-time", base.replace(/_/g, " ")];
  return `<span class="badge ${c}">${label}${/\(gap\)/.test(r) ? " (gap)" : ""}</span>`; }

let prev = null;
const flashRows = new Set(), flashLadders = new Set();

function toast(kind, k, msg) {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.innerHTML = `<div class="k">${k}</div>${msg}`;
  const box = $("toasts");
  box.appendChild(el);
  while (box.children.length > 6) box.firstChild.remove();
  setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 320); }, 4500);
}

function diffEvents(d) {
  if (!prev) return;   // first load: don't toast the whole backlog
  const pm = Object.fromEntries((prev.positions || []).map(p => [p.symbol, p]));
  for (const p of d.positions) {
    const w = pm[p.symbol];
    if (!w) { toast("open", "position opened", `${sideHtml(p.side)} <b>${esc(p.symbol)}</b> @ ${price(p.entry_price)}`); flashRows.add(p.symbol); }
    else if (p.fills > w.fills) { toast("fill", "rung filled", `<b>${esc(p.symbol)}</b> · ${p.fills}/${p.planned_rungs} filled`); flashRows.add(p.symbol); }
  }
  const qm = Object.fromEntries((prev.pending || []).map(q => [q.symbol, q]));
  for (const q of d.pending || []) {
    const w = qm[q.symbol];
    if (!w) { toast("open", "limit ladder placed", `${sideHtml(q.side)} <b>${esc(q.symbol)}</b> · ${q.total} rungs`); flashLadders.add(q.symbol); }
    else if (q.filled > w.filled) flashLadders.add(q.symbol);
  }
  const seen = new Set((prev.trades || []).map(t => t.symbol + ":" + t.exit_ts));
  for (const t of d.trades) {
    if (seen.has(t.symbol + ":" + t.exit_ts)) continue;
    const base = String(t.reason).replace(/ \(gap\)$/, "");
    const kind = base === "take_profit" ? "tp" : base === "stop_loss" || base === "liquidation" ? "sl"
      : base === "trailing_stop" ? "trail" : "fill";
    toast(kind, "closed · " + base.replace(/_/g, " "), `<b>${esc(t.symbol)}</b> ${usd(t.net_pnl, true)} <span class="${cls(t.r)}">(${t.r >= 0 ? "+" : "−"}${Math.abs(t.r).toFixed(2)}R)</span>`);
  }
}

function tile(label, value, hint, c) {
  return `<div class="tile"><div class="label">${label}</div><div class="value ${c || ""}">${value}</div>` +
         (hint ? `<div class="hint">${hint}</div>` : "") + `</div>`;
}

function render(d) {
  diffEvents(d);
  prev = d;
  $("strategy").textContent = `${d.strategy} · ${d.account_file}`;
  const st = $("status");
  if (!d.account) {
    st.innerHTML = `<span class="dot"></span><span>No account yet — start the bot</span>`;
    $("tiles").innerHTML = tile("Next decision", d.decision_seconds <= 2 ? "live" : `<span class="cd" data-until="${d.next_decision}"></span>`, cad(d.decision_seconds));
    $("chart").innerHTML = `<div class="empty">Nothing to show until the bot has run.</div>`;
    $("pendingCard").style.display = "none"; $("positions").innerHTML = $("trades").innerHTML = "";
    return;
  }
  const a = d.account, m = d.metrics, showConf = !!d.confidence_sizing;
  st.innerHTML = d.running
    ? `<span class="dot live"></span><span>Running</span>`
    : `<span class="dot down"></span><span>Not running — no data for ${ago(d.now - (d.last_update || 0))}</span>`;
  $("updated").textContent = d.last_update ? "last candle " + ago(Date.now() - d.last_update) + " ago" : "";

  $("tiles").innerHTML =
    tile("Equity", usd(a.equity), "started " + usd(a.starting_balance)) +
    tile("Return", pct(a.return_pct, true), "incl. open positions", cls(a.return_pct)) +
    tile("Open P&amp;L", usd(a.unrealized, true), d.positions.length + " open · " + (d.pending_entries || 0) + " working", cls(a.unrealized)) +
    tile("Win rate", m.trades ? (m.win_rate * 100).toFixed(0) + "%" : "—", m.trades ? m.trades + " trades · " + usd(m.net_pnl, true) + " net" : "none yet") +
    tile("Max drawdown", pct(-m.max_drawdown_pct), "from peak equity") +
    tile("Next decision", d.decision_seconds <= 2 ? "live" : `<span class="cd" data-until="${d.next_decision}"></span>`, cad(d.decision_seconds));

  drawChart(d.equity, a.starting_balance);

  const pend = d.pending || [];
  $("pendingCard").style.display = pend.length ? "" : "none";
  $("pendingCount").textContent = pend.length ? pend.length + " working" : "";
  $("pending").innerHTML = pend.length ? `<div class="ladders">` + pend.map(q => {
    const tq = q.rungs.reduce((s, r) => s + r.qty, 0) || 1;
    const rungs = q.rungs.map(r => `<div class="rung ${r.filled ? "filled" : "wait"}"><span class="mark"></span>
      <span>${r.market ? "market" : price(r.price)} <span class="wgt">${Math.round(r.qty / tq * 100)}%</span></span>
      <span class="dist">${r.filled ? "filled" : r.dist_pct == null ? "" : (r.dist_pct >= 0 ? "+" : "") + r.dist_pct.toFixed(2) + "%"}</span></div>`).join("");
    return `<div class="ladder" data-sym="${esc(q.symbol)}"><div class="lh"><span class="sym">${esc(q.symbol)}</span>${sideHtml(q.side)}
      <span class="meta">${q.filled}/${q.total} filled</span></div>${rungs}
      <div class="lbar"><i style="width:${q.total ? Math.round(q.filled / q.total * 100) : 0}%"></i></div>
      <div class="lfoot"><span>stop ${price(q.stop)}</span><span>target ${price(q.take_profit)}</span>
      ${showConf ? `<span>conf ${Math.round((q.conf || 0) * 100)}%</span>` : ""}
      <span>expires <span class="cd" data-until="${q.expiry_ts || 0}"></span></span></div></div>`;
  }).join("") + `</div>` : "";
  if (flashLadders.size) document.querySelectorAll("#pending .ladder[data-sym]").forEach(l => { if (flashLadders.has(l.dataset.sym)) l.classList.add("flash"); });
  flashLadders.clear();

  $("posCount").textContent = d.positions.length ? d.positions.length + " open" : "";
  $("positions").innerHTML = d.positions.length ? `<table><thead><tr><th class="l">Coin</th><th class="l">Side</th><th>Qty</th>
    <th>Entry</th><th>Last</th><th>Stop</th><th>Target</th><th>Fills</th>${showConf ? "<th>Conf</th>" : ""}<th>P&amp;L</th><th>Time&nbsp;stop</th></tr></thead><tbody>`
    + d.positions.map(p => `<tr data-sym="${esc(p.symbol)}"><td class="l">${esc(p.symbol)}</td><td class="l">${sideHtml(p.side)}</td><td>${p.qty}</td>
    <td>${price(p.entry_price)}</td><td>${price(p.last_price)}</td>
    <td>${price(p.stop)}${p.trailed ? ' <span class="lock" title="trailing stop active">🔒</span>' : ""}</td>
    <td>${price(p.take_profit)}</td><td>${p.fills}/${p.planned_rungs}</td>
    ${showConf ? `<td>${confHtml(p.conf)}</td>` : ""}
    <td class="${cls(p.upnl)}">${usd(p.upnl, true)} <span class="cd">(${p.upnl_r >= 0 ? "+" : "−"}${Math.abs(p.upnl_r).toFixed(2)}R)</span></td>
    <td><span class="cd" data-until="${p.time_stop_ts || 0}"></span></td></tr>`).join("") + `</tbody></table>`
    : `<div class="empty">No open positions.</div>`;
  if (flashRows.size) document.querySelectorAll("#positions tr[data-sym]").forEach(r => { if (flashRows.has(r.dataset.sym)) r.classList.add("flash-up"); });
  flashRows.clear();

  $("tradesCount").textContent = m.trades ? m.trades + " total" : "";
  $("trades").innerHTML = d.trades.length ? `<table><thead><tr><th class="l">Coin</th><th class="l">Side</th><th>Opened</th><th>Closed</th>
    <th>Entry</th><th>Exit</th>${showConf ? "<th>Conf</th>" : ""}<th>Net</th><th>R</th><th class="l">Exit</th></tr></thead><tbody>`
    + d.trades.map(t => `<tr><td class="l">${esc(t.symbol)}</td><td class="l">${sideHtml(t.side)}</td><td>${dt(t.entry_ts)}</td>
    <td>${dt(t.exit_ts)}</td><td>${price(t.entry_price)}</td><td>${price(t.exit_price)}</td>
    ${showConf ? `<td>${Math.round((t.conf || 0) * 100)}%</td>` : ""}
    <td class="${cls(t.net_pnl)}">${usd(t.net_pnl, true)}</td><td class="${cls(t.r)}">${t.r >= 0 ? "+" : "−"}${Math.abs(t.r).toFixed(2)}</td>
    <td class="l">${reasonBadge(t.reason)}</td></tr>`).join("") + `</tbody></table>`
    : `<div class="empty">No closed trades yet.</div>`;
  tick();
}

function tick() {
  const now = Date.now();
  document.querySelectorAll(".cd[data-until]").forEach(e => {
    const ts = +e.dataset.until;
    e.textContent = !ts ? "—" : ts - now <= 0 ? "now" : dur(ts - now);
  });
}

function drawChart(pts, startBal) {
  const box = $("chart");
  if (pts.length < 2) { box.innerHTML = `<div class="empty">The equity curve appears after the bot has run for a few minutes.</div>`; return; }
  const W = box.clientWidth || 600, H = 240, L = 56, R = 12, T = 10, B = 26;
  const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]).concat([startBal]);
  const x0 = xs[0], x1 = xs[xs.length - 1];
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  const pad = Math.max((y1 - y0) * 0.1, 0.5); y0 -= pad; y1 += pad;
  const sx = x => L + (x - x0) / (x1 - x0 || 1) * (W - L - R);
  const sy = y => T + (y1 - y) / (y1 - y0) * (H - T - B);
  // round tick step (1, 2, 2.5 or 5 × 10^n) giving about 4-6 gridlines
  const raw = (y1 - y0) / 4, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const stepT = [1, 2, 2.5, 5, 10].map(f => f * mag).find(s => s >= raw * 0.7);
  const dec = stepT >= 1 ? 0 : Math.min(2, Math.ceil(-Math.log10(stepT)));
  const grid = [];
  for (let v = Math.ceil(y0 / stepT) * stepT; v <= y1 + 1e-9; v += stepT) {
    const y = sy(v).toFixed(1);
    grid.push(`<line x1="${L}" x2="${W - R}" y1="${y}" y2="${y}" stroke="var(--grid)"/>` +
              `<text x="${L - 6}" y="${+y + 4}" text-anchor="end">$${v.toFixed(dec)}</text>`);
  }
  const xl = [];
  const nx = Math.max(2, Math.min(5, Math.floor((W - L - R) / 120)));
  for (let i = 0; i < nx; i++) {
    const t = x0 + (x1 - x0) * i / (nx - 1);
    xl.push(`<text x="${sx(t).toFixed(1)}" y="${H - 6}" text-anchor="${i === 0 ? "start" : i === nx - 1 ? "end" : "middle"}">${dt(t)}</text>`);
  }
  const path = pts.map((p, i) => (i ? "L" : "M") + sx(p[0]).toFixed(1) + "," + sy(p[1]).toFixed(1)).join("");
  const by = sy(startBal).toFixed(1);
  box.innerHTML = `<svg width="${W}" height="${H}" role="img" aria-label="Equity over time">
    ${grid.join("")}${xl.join("")}
    <line x1="${L}" x2="${W - R}" y1="${by}" y2="${by}" stroke="var(--text-muted)" stroke-dasharray="4 4"/>
    <text x="${W - R}" y="${by - 4}" text-anchor="end">start</text>
    <path d="${path}" fill="none" stroke="var(--series-1)" stroke-width="2" stroke-linejoin="round"/>
    <g id="hover" style="display:none"><line id="hx" y1="${T}" y2="${H - B}" stroke="var(--text-muted)"/>
      <circle id="hc" r="4.5" fill="var(--series-1)" stroke="var(--surface-1)" stroke-width="2"/></g>
    <rect x="${L}" y="0" width="${W - L - R}" height="${H}" fill="transparent" id="hit"/>
  </svg><div class="tip" id="tip"></div>`;
  const hit = $("hit"), g = $("hover"), tip = $("tip");
  const move = ev => {
    const r = hit.getBoundingClientRect(), cx = (ev.touches ? ev.touches[0].clientX : ev.clientX) - r.left + L;
    let i = 0, best = Infinity;
    pts.forEach((p, j) => { const dd = Math.abs(sx(p[0]) - cx); if (dd < best) { best = dd; i = j; } });
    const px = sx(pts[i][0]), py = sy(pts[i][1]);
    g.style.display = ""; $("hx").setAttribute("x1", px); $("hx").setAttribute("x2", px);
    $("hc").setAttribute("cx", px); $("hc").setAttribute("cy", py);
    tip.style.display = "block";
    tip.innerHTML = `${dt(pts[i][0])}<br><b>${usd(pts[i][1])}</b> <span class="${cls(pts[i][1] - startBal)}">(${usd(pts[i][1] - startBal, true)})</span>`;
    tip.style.left = Math.min(px + 10, W - tip.offsetWidth - 4) + "px"; tip.style.top = Math.max(py - 50, 0) + "px";
  };
  const leave = () => { g.style.display = "none"; tip.style.display = "none"; };
  hit.addEventListener("mousemove", move); hit.addEventListener("touchmove", move, {passive: true});
  hit.addEventListener("mouseleave", leave); hit.addEventListener("touchend", leave);
}

let paused = false;
async function refresh(force) {
  if (paused && !force) return;
  try {
    const r = await fetch("api/status", {cache: "no-store"});
    if (!r.ok) throw new Error("HTTP " + r.status);
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    $("error").style.display = "none";
    render(d);
  } catch (e) {
    $("error").style.display = "block";
    $("error").textContent = "Could not load the account: " + e.message;
  }
}
$("pause").onclick = () => { paused = !paused; $("pause").textContent = paused ? "▶ Resume" : "⏸ Pause"; if (!paused) refresh(true); };
$("refreshNow").onclick = () => refresh(true);
refresh(true);
setInterval(refresh, __REFRESH_MS__);
setInterval(tick, 500);   // smooth countdowns between polls
let rt; addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => prev && render(prev), 150); });
</script>
</body>
</html>
"""
