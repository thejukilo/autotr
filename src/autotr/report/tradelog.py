"""Trade log persistence: CSV for backtests, SQLite for live runs."""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from autotr.models import EquityPoint, Side, Trade

TRADE_FIELDS = ["symbol", "side", "qty", "entry_price", "stop", "take_profit", "exit_price", "entry_time", "exit_time",
                "gross_pnl", "fees", "funding", "net_pnl", "r_multiple", "risk_usd", "exit_reason",
                "entry_reason"]


def iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def trade_row(t: Trade) -> dict:
    return {
        "symbol": t.symbol, "side": t.side.value, "qty": t.qty, "entry_price": t.entry_price,
        "stop": t.stop, "take_profit": t.take_profit,
        "exit_price": t.exit_price, "entry_time": iso(t.entry_ts), "exit_time": iso(t.exit_ts),
        "gross_pnl": round(t.gross_pnl, 6), "fees": round(t.fees, 6), "funding": round(t.funding, 6),
        "net_pnl": round(t.net_pnl, 6), "r_multiple": round(t.r_multiple, 3),
        "risk_usd": round(t.risk_usd, 4), "exit_reason": t.exit_reason, "entry_reason": t.entry_reason,
    }


def write_trades_csv(trades: list[Trade], path: str | Path) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=TRADE_FIELDS)
        w.writeheader()
        for t in trades:
            w.writerow(trade_row(t))


class LiveStore:
    """SQLite store for the live paper account: trades, equity snapshots and engine state."""

    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.executescript(
            """
            PRAGMA journal_mode = WAL;
            PRAGMA synchronous = NORMAL;
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT, side TEXT, qty REAL, entry_price REAL, exit_price REAL,
                entry_ts INTEGER, exit_ts INTEGER, gross_pnl REAL, fees REAL, funding REAL,
                exit_reason TEXT, risk_usd REAL, entry_reason TEXT
            );
            CREATE TABLE IF NOT EXISTS equity (
                ts INTEGER PRIMARY KEY, equity REAL, balance REAL, open_positions INTEGER
            );
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        self.db.commit()

    def add_trade(self, t: Trade) -> None:
        self.db.execute(
            "INSERT INTO trades (symbol, side, qty, entry_price, exit_price, entry_ts, exit_ts, gross_pnl,"
            " fees, funding, exit_reason, risk_usd, entry_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t.symbol, t.side.value, t.qty, t.entry_price, t.exit_price, t.entry_ts, t.exit_ts,
             t.gross_pnl, t.fees, t.funding, t.exit_reason, t.risk_usd, t.entry_reason),
        )
        self.db.commit()

    def trades(self) -> list[Trade]:
        rows = self.db.execute(
            "SELECT symbol, side, qty, entry_price, exit_price, entry_ts, exit_ts, gross_pnl, fees, funding,"
            " exit_reason, risk_usd, entry_reason FROM trades ORDER BY id").fetchall()
        return [Trade(r[0], Side(r[1]), *r[2:]) for r in rows]

    def add_equity(self, p: EquityPoint) -> None:
        self.db.execute("INSERT OR REPLACE INTO equity VALUES (?,?,?,?)",
                        (p.ts, p.equity, p.balance, p.open_positions))
        self.db.commit()

    def equity(self) -> list[EquityPoint]:
        return [EquityPoint(*r) for r in self.db.execute("SELECT ts, equity, balance, open_positions FROM equity ORDER BY ts")]

    def save_state(self, key: str, value: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (key, json.dumps(value)))
        self.db.commit()

    def load_state(self, key: str) -> dict | None:
        row = self.db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def close(self) -> None:
        self.db.close()
