"""'Top movers' universe for scalping research: the coins with the biggest daily moves, per day.

Backtests on a fixed coin list can't test a scalper that trades whatever is pumping today. This
module rebuilds that universe from history without survivorship bias:

1. List **every** USDT perpetual that ever had data on data.binance.vision (delisted ones included).
2. Download their daily candles for the months needed and keep, per day, the `top_n` coins by
   |daily change| with at least `min_quote_volume` of daily volume.
3. Download 1m candles (+ funding) for each selected coin from `days_before` days before to one
   day after each day it was selected, so the 24h scanner and indicators can warm up.

At run time the normal scanner (`rank_by: change_24h`) then picks the live-style top movers among
these coins. Symbols whose data has a gap are skipped by the scanner until they have a fresh 24h
history again (see `Scanner.scan`).

Index: `<data dir>/movers/index.json` = {"YYYY-MM-DD": [symbols...]}.
"""

from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

import httpx
import pandas as pd

from autotr.data.historical import BASE_URL, HistoricalStore, date_range

log = logging.getLogger(__name__)

S3_LIST_URL = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
DAILY_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume"]


def movers_dir(data_dir: str | Path) -> Path:
    return Path(data_dir) / "movers"


def list_all_usdt_perps(client: httpx.Client) -> list[str]:
    """Every USDT-margined symbol with kline history on data.binance.vision (incl. delisted)."""
    prefix = "data/futures/um/monthly/klines/"
    out: list[str] = []
    marker = ""
    while True:
        r = client.get(S3_LIST_URL, params={"prefix": prefix, "delimiter": "/", "marker": marker}, timeout=30)
        r.raise_for_status()
        names = re.findall(r"<Prefix>" + re.escape(prefix) + r"([^/<]+)/</Prefix>", r.text)
        out += names
        nxt = re.search(r"<NextMarker>([^<]+)</NextMarker>", r.text)
        if "<IsTruncated>true</IsTruncated>" not in r.text or not nxt:
            break
        marker = nxt.group(1)
    return sorted(s for s in set(out) if s.endswith("USDT") and re.fullmatch(r"[A-Z0-9]+", s))


def _parse_daily(content: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        raw = zf.read(zf.namelist()[0]).decode()
    lines = [ln for ln in raw.splitlines() if ln and ln[0].isdigit()]  # drop optional header
    rows = [ln.split(",")[:8] for ln in lines]
    df = pd.DataFrame(rows, columns=DAILY_COLS)
    df = df.astype({"open_time": "int64", "open": float, "high": float, "low": float, "close": float,
                    "volume": float, "quote_volume": float})
    df["day"] = pd.to_datetime(df["open_time"], unit="ms").dt.date.astype(str)
    return df[["day", "open", "high", "low", "close", "quote_volume"]]


class MoversStore:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.root = movers_dir(data_dir)
        self.daily_dir = self.root / "daily"
        self.index_path = self.root / "index.json"

    # ---------------------------------------------------------------- daily candles
    def _fetch(self, client: httpx.Client, url: str) -> bytes | None:
        for attempt in range(3):
            try:
                r = client.get(url, timeout=30)
                if r.status_code == 404:
                    return None
                r.raise_for_status()
                return r.content
            except httpx.HTTPError as exc:
                log.warning("fetch failed (%s/3) %s: %s", attempt + 1, url, exc)
        return None

    def _daily_month(self, client: httpx.Client, sym: str, y: int, m: int, last_day: date) -> pd.DataFrame | None:
        path = self.daily_dir / sym / f"{y:04d}-{m:02d}.parquet"
        miss = path.with_suffix(".missing")
        if path.exists():
            return pd.read_parquet(path)
        if miss.exists():
            return None
        content = self._fetch(client, f"{BASE_URL}/monthly/klines/{sym}/1d/{sym}-1d-{y:04d}-{m:02d}.zip")
        if content is not None:
            df = _parse_daily(content)
        else:
            # Month not published yet (current month) or symbol not listed then: try daily files.
            first = date(y, m, 1)
            days = [d for d in date_range(first, last_day) if d.month == m]
            frames = []
            for d in days:
                c = self._fetch(client, f"{BASE_URL}/daily/klines/{sym}/1d/{sym}-1d-{d.isoformat()}.zip")
                if c is not None:
                    frames.append(_parse_daily(c))
                elif not frames:
                    break  # not listed at the start of the month: don't probe every day
            if not frames:
                path.parent.mkdir(parents=True, exist_ok=True)
                miss.touch()
                return None
            df = pd.concat(frames, ignore_index=True)
            complete = days and days[-1].month != (last_day + timedelta(days=1)).month
            if not complete:
                return df  # partial month: don't cache, it will grow
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(path, index=False)
        return df

    def daily_table(self, symbols: list[str], start: date, end: date, workers: int = 16) -> pd.DataFrame:
        months = sorted({(d.year, d.month) for d in date_range(start, end)})
        frames: list[pd.DataFrame] = []
        with httpx.Client(follow_redirects=True) as client, ThreadPoolExecutor(workers) as pool:
            futs = {pool.submit(self._daily_month, client, s, y, m, end): s for s in symbols for y, m in months}
            for i, fut in enumerate(as_completed(futs), 1):
                df = fut.result()
                if df is not None and len(df):
                    frames.append(df.assign(symbol=futs[fut]))
                if i % 500 == 0:
                    log.info("daily candles: %d/%d", i, len(futs))
        if not frames:
            return pd.DataFrame(columns=["symbol", "day", "open", "high", "low", "close", "quote_volume"])
        df = pd.concat(frames, ignore_index=True)
        return df[(df["day"] >= start.isoformat()) & (df["day"] <= end.isoformat())]

    # ---------------------------------------------------------------- selection
    @staticmethod
    def select(daily: pd.DataFrame, top_n: int, min_quote_volume: float) -> dict[str, list[str]]:
        d = daily[(daily["quote_volume"] >= min_quote_volume) & (daily["open"] > 0)].copy()
        d["abs_change"] = (d["close"] / d["open"] - 1).abs()
        out: dict[str, list[str]] = {}
        for day, g in d.groupby("day"):
            out[str(day)] = g.nlargest(top_n, "abs_change")["symbol"].tolist()
        return out

    def load_index(self) -> dict[str, list[str]]:
        return json.loads(self.index_path.read_text()) if self.index_path.exists() else {}

    def save_index(self, index: dict[str, list[str]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        merged = {**self.load_index(), **index}
        self.index_path.write_text(json.dumps(dict(sorted(merged.items())), indent=0))

    # ---------------------------------------------------------------- 1m data
    def download_windows(self, windows: list[tuple[date, date]], top_n: int = 20,
                         min_quote_volume: float = 20_000_000, days_before: int = 2,
                         workers: int = 8) -> dict:
        """Select the movers for every day of the windows and download their 1m candles + funding."""
        lo = min(s for s, _ in windows) - timedelta(days=days_before + 1)
        hi = max(e for _, e in windows)
        with httpx.Client(follow_redirects=True) as client:
            symbols = list_all_usdt_perps(client)
        log.info("%d USDT perpetuals (incl. delisted) on data.binance.vision", len(symbols))
        daily = self.daily_table(symbols, lo, hi)
        index = self.select(daily, top_n, min_quote_volume)
        wanted_days = {d.isoformat() for s, e in windows for d in date_range(s - timedelta(days=days_before), e)}
        index = {d: syms for d, syms in index.items() if d in wanted_days}
        self.save_index(index)

        need: dict[str, set[date]] = {}
        for day_s, syms in index.items():
            d = date.fromisoformat(day_s)
            for sym in syms:
                need.setdefault(sym, set()).update(d + timedelta(days=k) for k in range(-days_before, 2))
        store = HistoricalStore(self.data_dir)
        stats = {"downloaded": 0, "cached": 0, "missing": 0}
        jobs = [(sym, d) for sym, days in need.items() for d in sorted(days) if d <= hi]
        months = {(sym, d.year, d.month) for sym, d in jobs}
        log.info("downloading %d coin-days of 1m candles for %d symbols", len(jobs), len(need))
        with httpx.Client(follow_redirects=True) as client, ThreadPoolExecutor(workers) as pool:
            futs = [pool.submit(store._download_kline_day, client, sym, d) for sym, d in jobs]
            futs += [pool.submit(store._download_funding_month, client, sym, y, m) for sym, y, m in months]
            for i, fut in enumerate(as_completed(futs), 1):
                try:
                    stats[fut.result()] += 1
                except RuntimeError as exc:
                    log.warning("%s", exc)
                    stats["missing"] += 1
                if i % 1000 == 0:
                    log.info("1m data: %d/%d", i, len(futs))
        return {**stats, "symbols": len(need), "days": len(index)}


def movers_symbols(data_dir: str | Path, start: date, end: date, days_before: int = 2) -> list[str]:
    """Every symbol selected as a mover on any day of [start - days_before, end]."""
    index = MoversStore(data_dir).load_index()
    if not index:
        raise RuntimeError("no movers index — run `autotr download-movers` first")
    lo, hi = (start - timedelta(days=days_before)).isoformat(), end.isoformat()
    return sorted({s for d, syms in index.items() if lo <= d <= hi for s in syms})
