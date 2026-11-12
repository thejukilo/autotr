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
            " COALESCE(fills, 1) FROM trades ORDER BY id").fetchall()
        trades = [Trade(r[0], Side(r[1]), *r[2:]) for r in rows]
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
    tf_ms = cfg.strategy.entry_tf_minutes * MINUTE_MS
    base = {
        "now": now_ms,
        "strategy": cfg.strategy.name,
        "account_file": db_path.name,
        "next_decision": (now_ms // tf_ms + 1) * tf_ms,
        "decision_every_min": cfg.strategy.entry_tf_minutes,
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
        positions.append({
            "symbol": p["symbol"], "side": p["side"], "qty": p["qty"], "entry_price": p["entry_price"],
            "last_price": last, "stop": p["stop"], "take_profit": p["take_profit"], "entry_ts": p["entry_ts"],
            "margin": p["margin"], "risk_usd": p["risk_usd"],
            "upnl": sign * (last - p["entry_price"]) * p["qty"],
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
        "pending_entries": len(ex.get("pending", [])),
        "metrics": {k: m[k] for k in ("trades", "win_rate", "net_pnl", "fees", "funding", "profit_factor",
                                      "expectancy_r", "max_drawdown_pct", "by_side", "per_symbol")},
        "positions": positions,
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
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
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
  --surface-0: #f4f4f2; --surface-1: #fcfcfb; --border: #e3e2de;
  --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #7a7974;
  --grid: #ecebe7; --series-1: #2a78d6;
  --good: #0ca30c; --good-text: #006300; --critical: #d03b3b; --critical-text: #b42f2f;
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --surface-0: #111110; --surface-1: #1a1a19; --border: #2c2c2a;
    --text-primary: #f0efec; --text-secondary: #c3c2b7; --text-muted: #8f8e86;
    --grid: #2a2a28; --series-1: #3987e5;
    --good-text: #4cc74c; --critical-text: #ef7a7a;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--surface-0); color: var(--text-primary);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 20px 16px 40px; }
header { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px 16px; margin-bottom: 16px; }
h1 { font-size: 18px; margin: 0; font-weight: 600; }
h2 { font-size: 14px; margin: 0 0 10px; font-weight: 600; }
.sub { color: var(--text-secondary); }
.pill { display: inline-flex; align-items: center; gap: 6px; padding: 2px 10px; border-radius: 999px;
  border: 1px solid var(--border); background: var(--surface-1); font-size: 13px; }
.dot { width: 8px; height: 8px; border-radius: 50%; }
.card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px;
  margin-bottom: 14px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-bottom: 14px; }
.tile { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; }
.tile .label { color: var(--text-secondary); font-size: 12px; }
.tile .value { font-size: 22px; font-weight: 600; font-variant-numeric: tabular-nums; margin-top: 2px; }
.tile .hint { color: var(--text-muted); font-size: 12px; margin-top: 2px; }
.pos { color: var(--good-text); } .neg { color: var(--critical-text); }
.chart-wrap { position: relative; }
svg text { fill: var(--text-muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.tip { position: absolute; pointer-events: none; background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 6px; padding: 6px 8px; font-size: 12px; white-space: nowrap; box-shadow: 0 2px 8px rgba(0,0,0,.12);
  display: none; }
.tip b { font-variant-numeric: tabular-nums; }
.scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { padding: 6px 8px; text-align: right; white-space: nowrap; border-bottom: 1px solid var(--border); }
th { color: var(--text-secondary); font-weight: 500; font-size: 12px; }
th:first-child, td:first-child, th.l, td.l { text-align: left; }
tr:last-child td { border-bottom: 0; }
.empty { color: var(--text-muted); padding: 8px 0; }
footer { color: var(--text-muted); font-size: 12px; margin-top: 20px; }
</style>
</head>
<body>
<main>
  <header>
    <h1>autotr</h1>
    <span class="sub" id="strategy"></span>
    <span class="pill" id="status"><span class="dot"></span><span>Loading…</span></span>
    <span class="sub" id="updated"></span>
  </header>
  <div id="error" class="card" style="display:none"></div>
  <div class="tiles" id="tiles"></div>
  <section class="card">
    <h2>Equity</h2>
    <div class="chart-wrap" id="chart"></div>
  </section>
  <section class="card">
    <h2>Open positions</h2>
    <div class="scroll" id="positions"></div>
  </section>
  <section class="card">
    <h2>Closed trades</h2>
    <div class="scroll" id="trades"></div>
  </section>
  <footer>Paper trading only: simulated orders on real Binance prices, no real money. Refreshes every 30 s.</footer>
</main>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const usd = (v, sign) => (sign && v > 0 ? "+" : v < 0 ? "−" : "") + "$" + Math.abs(v).toFixed(2);
const pct = (v, sign) => (sign && v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(2) + "%";
const cls = v => v > 0 ? "pos" : v < 0 ? "neg" : "";
const price = v => v >= 100 ? v.toFixed(2) : v >= 1 ? v.toFixed(4) : v.toPrecision(4);
const dt = ts => new Date(ts).toLocaleString(undefined, {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"});
const ago = ms => { const s = Math.max(0, Math.round(ms / 1000));
  return s < 90 ? s + " s" : s < 5400 ? Math.round(s / 60) + " min" : s < 172800 ? Math.round(s / 3600) + " h" : Math.round(s / 86400) + " days"; };
const until = ms => { const m = Math.max(0, Math.round(ms / 60000)); return m < 60 ? m + " min" : Math.floor(m / 60) + " h " + (m % 60) + " min"; };
let last = null;

function tile(label, value, hint, c) {
  return `<div class="tile"><div class="label">${label}</div><div class="value ${c || ""}">${value}</div>` +
         (hint ? `<div class="hint">${hint}</div>` : "") + `</div>`;
}

function render(d) {
  last = d;
  $("strategy").textContent = `${d.strategy} · ${d.account_file}`;
  const st = $("status");
  if (!d.account) {
    st.innerHTML = `<span class="dot" style="background:var(--text-muted)"></span><span>No account yet: start the bot</span>`;
    $("tiles").innerHTML = tile("Next decision", new Date(d.next_decision).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}),
                                "in " + until(d.next_decision - Date.now()));
    $("chart").innerHTML = `<div class="empty">Nothing to show until the bot has run.</div>`;
    $("positions").innerHTML = $("trades").innerHTML = "";
    return;
  }
  const a = d.account, m = d.metrics;
  st.innerHTML = d.running
    ? `<span class="dot" style="background:var(--good)"></span><span>✓ Running</span>`
    : `<span class="dot" style="background:var(--critical)"></span><span>✕ Not running: no market data for ${ago(d.now - (d.last_update || 0))}</span>`;
  $("updated").textContent = d.last_update ? "last candle " + ago(Date.now() - d.last_update) + " ago" : "";

  $("tiles").innerHTML =
    tile("Equity", usd(a.equity), "started with " + usd(a.starting_balance) + " · " + dt(a.since)) +
    tile("Return", pct(a.return_pct, true), "incl. open positions", cls(a.return_pct)) +
    tile("Open P&amp;L", usd(a.unrealized, true), d.positions.length + " open" + (d.pending_entries ? ", " + d.pending_entries + " pending" : ""), cls(a.unrealized)) +
    tile("Closed trades", m.trades, m.trades ? (m.win_rate * 100).toFixed(0) + "% win · " + usd(m.net_pnl, true) + " net" : "none yet") +
    tile("Max drawdown", pct(-m.max_drawdown_pct), "from peak equity") +
    tile("Next decision", new Date(d.next_decision).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}),
         "in " + until(d.next_decision - Date.now()) + " · every " + (d.decision_every_min / 60) + " h");

  drawChart(d.equity, a.starting_balance);

  $("positions").innerHTML = d.positions.length ? `<table><tr><th>Coin</th><th class="l">Side</th><th>Qty</th><th>Entry</th>
    <th>Last</th><th>Stop</th><th>P&amp;L</th><th>Opened</th></tr>` + d.positions.map(p => `<tr>
    <td>${esc(p.symbol)}</td><td class="l">${p.side === "LONG" ? "▲ long" : "▼ short"}</td><td>${p.qty}</td>
    <td>${price(p.entry_price)}</td><td>${price(p.last_price)}</td><td>${price(p.stop)}</td>
    <td class="${cls(p.upnl)}">${usd(p.upnl, true)}</td><td>${dt(p.entry_ts)}</td></tr>`).join("") + `</table>`
    : `<div class="empty">No open positions.</div>`;

  $("trades").innerHTML = d.trades.length ? `<table><tr><th>Coin</th><th class="l">Side</th><th>Opened</th><th>Closed</th>
    <th>Entry</th><th>Exit</th><th>Net</th><th>R</th><th class="l">Exit reason</th></tr>` + d.trades.map(t => `<tr>
    <td>${esc(t.symbol)}</td><td class="l">${t.side === "LONG" ? "▲ long" : "▼ short"}</td><td>${dt(t.entry_ts)}</td>
    <td>${dt(t.exit_ts)}</td><td>${price(t.entry_price)}</td><td>${price(t.exit_price)}</td>
    <td class="${cls(t.net_pnl)}">${usd(t.net_pnl, true)}</td><td>${t.r >= 0 ? "+" : "−"}${Math.abs(t.r).toFixed(2)}</td>
    <td class="l">${esc(t.reason)}</td></tr>`).join("") + `</table>`
    : `<div class="empty">No closed trades yet. Expect about one a day.</div>`;
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

async function refresh() {
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
refresh();
setInterval(refresh, 30000);
let rt; addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => last && render(last), 150); });
</script>
</body>
</html>
"""
