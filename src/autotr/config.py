"""Typed configuration loaded from YAML."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "default.yaml"


class AccountCfg(BaseModel):
    starting_balance: float = 100.0


class ExchangeCfg(BaseModel):
    taker_fee: float = 0.0005
    maker_fee: float = 0.0002
    slippage_bps: float = 2.0
    leverage: int = 5
    maintenance_margin_rate: float = 0.01
    default_funding_rate: float = 0.0001


class DataCfg(BaseModel):
    dir: str = "data"
    interval: str = "1m"


class UniverseCfg(BaseModel):
    symbols: list[str] = []
    live_candidates: int = 40


class ScannerCfg(BaseModel):
    min_quote_volume_24h: float = 50_000_000
    atr_period_bars: int = 48
    top_n: int = 8
    rescan_minutes: int = 15


class StrategyCfg(BaseModel):
    rsi_period: int = 14
    rsi_oversold: float = 30
    rsi_overbought: float = 70
    atr_period: int = 14
    pivot_left: int = 3
    pivot_right: int = 3
    history_5m_bars: int = 288
    zone_tolerance_atr: float = 0.25
    zone_min_touches: int = 2
    trendline_lookback_pivots: int = 6
    trendline_break_tolerance_atr: float = 0.1
    proximity_atr: float = 0.3
    touch_lookback_bars: int = 3
    stop_buffer_atr: float = 0.5
    min_stop_pct: float = 0.0015
    max_stop_pct: float = 0.03
    take_profit_r: float = 2.0
    min_reward_risk_after_fees: float = 1.2
    time_stop_minutes: int = 30
    cooldown_minutes: int = 5


class RiskCfg(BaseModel):
    risk_per_trade: float = 0.02
    max_margin_per_position: float = 0.5
    max_positions: int = 2
    daily_loss_limit: float = 0.06
    max_trades_per_day: int = 40


class LiveCfg(BaseModel):
    db_path: str = "data/live.db"
    candidate_refresh_hours: float = 6
    warmup_bars: int = 1500
    rest_url: str = "https://fapi.binance.com"
    ws_url: str = "wss://fstream.binance.com"
    stream_timeout_s: float = 150


class Config(BaseModel):
    account: AccountCfg = AccountCfg()
    exchange: ExchangeCfg = ExchangeCfg()
    data: DataCfg = DataCfg()
    universe: UniverseCfg = UniverseCfg()
    scanner: ScannerCfg = ScannerCfg()
    strategy: StrategyCfg = StrategyCfg()
    risk: RiskCfg = RiskCfg()
    live: LiveCfg = LiveCfg()


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | Path | None = None) -> Config:
    """Load default.yaml, then deep-merge an optional override file on top."""
    raw: dict[str, Any] = {}
    if DEFAULT_CONFIG_PATH.exists():
        raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text()) or {}
    if path is not None:
        override = yaml.safe_load(Path(path).read_text()) or {}
        raw = _deep_merge(raw, override)
    return Config.model_validate(raw)
