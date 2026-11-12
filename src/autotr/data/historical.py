"""Download and cache Binance USDT-M futures history from data.binance.vision.

Layout of the cache (all parquet):
    {data_dir}/klines/{SYMBOL}/{YYYY-MM-DD}.parquet   one file per UTC day of 1m bars
    {data_dir}/funding/{SYMBOL}/{YYYY-MM}.parquet      one file per month of funding events
Days/months that do not exist upstream are remembered in a `.missing` marker so they are not
re-requested on every run.
"""

from __future__ import annotations

import io
import logging
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pandas as pd

from autotr.models import HOUR_MS

log = logging.getLogger(__name__)

BASE_URL = "https://data.binance.vision/data/futures/um"

KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume", "ignore",
]
KEEP_KLINE_COLUMNS = ["open_time", "open", "high", "low", "close", "volume", "quote_volume"]


def default_end_date() -> date:
    """Latest day that is normally published (daily dumps lag ~1–2 days)."""
    return datetime.now(timezone.utc).date() - timedelta(days=2)


def date_range(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _read_zip_csv(content: bytes, columns: list[str]) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        name = zf.namelist()[0]
        with zf.open(name) as fh:
            raw = fh.read()
    # Older dumps have no header row; newer ones do. Detect by first character.
    has_header = not raw[:1].isdigit()
    return pd.read_csv(
        io.BytesIO(raw),
        header=0 if has_header else None,
        names=None if has_header else columns,
    )


def parse_klines(content: bytes) -> pd.DataFrame:
    df = _read_zip_csv(content, KLINE_COLUMNS)
    df = df[KEEP_KLINE_COLUMNS].copy()
    df["open_time"] = df["open_time"].astype("int64")
    for c in KEEP_KLINE_COLUMNS[1:]:
        df[c] = df[c].astype("float64")
    return df.sort_values("open_time").reset_index(drop=True)


def parse_funding(content: bytes) -> pd.DataFrame:
    df = _read_zip_csv(content, ["calc_time", "funding_interval_hours", "last_funding_rate"])
    out = pd.DataFrame(
        {
            # calc_time carries a few ms of jitter; snap to the minute
            "time": (df["calc_time"].astype("int64") // 60_000) * 60_000,
            "rate": df["last_funding_rate"].astype("float64"),
        }
    )
    return out.sort_values("time").reset_index(drop=True)


class HistoricalStore:
    def __init__(self, data_dir: str | Path):
        self.root = Path(data_dir)

    # ---------- paths ----------
    def kline_path(self, symbol: str, day: date) -> Path:
        return self.root / "klines" / symbol / f"{day.isoformat()}.parquet"

    def funding_path(self, symbol: str, year: int, month: int) -> Path:
        return self.root / "funding" / symbol / f"{year:04d}-{month:02d}.parquet"

    def _missing_marker(self, path: Path) -> Path:
        return path.with_suffix(".missing")

    # ---------- download ----------
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
        raise RuntimeError(f"giving up on {url}")

    def _download_kline_day(self, client: httpx.Client, symbol: str, day: date) -> str:
        path = self.kline_path(symbol, day)
        if path.exists() or self._missing_marker(path).exists():
            return "cached"
        url = f"{BASE_URL}/daily/klines/{symbol}/1m/{symbol}-1m-{day.isoformat()}.zip"
        content = self._fetch(client, url)
        path.parent.mkdir(parents=True, exist_ok=True)
        if content is None:
            # Only remember "missing" for days that are definitely in the past.
            if day < default_end_date():
                self._missing_marker(path).touch()
            return "missing"
        parse_klines(content).to_parquet(path, index=False)
        return "downloaded"

    def _download_funding_month(self, client: httpx.Client, symbol: str, year: int, month: int) -> str:
        path = self.funding_path(symbol, year, month)
        if path.exists():
            return "cached"
        url = f"{BASE_URL}/monthly/fundingRate/{symbol}/{symbol}-fundingRate-{year:04d}-{month:02d}.zip"
        content = self._fetch(client, url)
        if content is None:
            return "missing"  # current month isn't published until it ends
        path.parent.mkdir(parents=True, exist_ok=True)
        parse_funding(content).to_parquet(path, index=False)
        return "downloaded"

    def download(self, symbols: list[str], start: date, end: date, workers: int = 8) -> dict[str, int]:
        days = date_range(start, end)
        months = sorted({(d.year, d.month) for d in days})
        stats: dict[str, int] = {"downloaded": 0, "cached": 0, "missing": 0}
        with httpx.Client(follow_redirects=True) as client, ThreadPoolExecutor(workers) as pool:
            futures = [pool.submit(self._download_kline_day, client, s, d) for s in symbols for d in days]
            futures += [pool.submit(self._download_funding_month, client, s, y, m) for s in symbols for y, m in months]
            for fut in as_completed(futures):
                stats[fut.result()] += 1
        return stats

    # ---------- load ----------
    def load_klines(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        frames = [pd.read_parquet(p) for d in date_range(start, end) if (p := self.kline_path(symbol, d)).exists()]
        if not frames:
            return pd.DataFrame(columns=KEEP_KLINE_COLUMNS)
        df = pd.concat(frames, ignore_index=True)
        return df.drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)

    def load_funding(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        months = sorted({(d.year, d.month) for d in date_range(start, end)})
        frames = [pd.read_parquet(p) for y, m in months if (p := self.funding_path(symbol, y, m)).exists()]
        if not frames:
            return pd.DataFrame(columns=["time", "rate"])
        return pd.concat(frames, ignore_index=True).sort_values("time").reset_index(drop=True)

    def available_symbols(self) -> list[str]:
        kdir = self.root / "klines"
        return sorted(p.name for p in kdir.iterdir() if p.is_dir()) if kdir.exists() else []




def funding_schedule(store: HistoricalStore, symbol: str, start: date, end: date,
                      start_ts: int, end_ts: int, default_rate: float) -> tuple[dict[int, float], str]:
    """Funding events {minute_ts: rate} for [start_ts, end_ts]. Published history where available;
    for months not yet published, the default rate every 8h (00/08/16 UTC)."""
    df = store.load_funding(symbol, start, end)
    events: dict[int, float] = {}
    covered_until = start_ts
    if len(df):
        for t, r in zip(df["time"].to_numpy(), df["rate"].to_numpy()):
            if start_ts <= t <= end_ts:
                events[int(t)] = float(r)
        covered_until = int(df["time"].max())
    # Months not yet published: assume the default rate every 8h (00/08/16 UTC).
    t = (max(covered_until, start_ts) // (8 * HOUR_MS) + 1) * 8 * HOUR_MS
    filled = 0
    while t <= end_ts:
        events.setdefault(t, default_rate)
        t += 8 * HOUR_MS
        filled += 1
    source = "historical" if filled == 0 else ("default" if not len(df) else "historical+default")
    return events, source
