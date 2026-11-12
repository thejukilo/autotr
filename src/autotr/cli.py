"""Command-line interface: `autotr download | backtest | fetch-exchange-info | live | status`."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import typer

from autotr.config import load_config

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__)

ConfigOpt = typer.Option(None, "--config", "-c", help="YAML file overriding config/default.yaml")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def _symbols(arg: Optional[str], default: list[str]) -> list[str]:
    return [s.strip().upper() for s in arg.split(",")] if arg else default


@app.command()
def download(
    days: int = typer.Option(30, help="Number of days to fetch (ending at --end)"),
    end: Optional[str] = typer.Option(None, help="Last day YYYY-MM-DD (default: 2 days ago, latest published)"),
    symbols: Optional[str] = typer.Option(None, help="Comma-separated symbols (default: config universe)"),
    config: Optional[Path] = ConfigOpt,
) -> None:
    """Download 1m klines + funding rates from data.binance.vision into the local cache."""
    from autotr.data.historical import HistoricalStore, default_end_date

    _setup_logging(False)
    cfg = load_config(config)
    end_d = _parse_date(end) if end else default_end_date()
    # +1 day for indicator / scanner warm-up before the first backtest day
    start_d = end_d - timedelta(days=days)
    syms = _symbols(symbols, cfg.universe.symbols)
    typer.echo(f"Downloading {len(syms)} symbols, {start_d} .. {end_d} into {cfg.data.dir}/")
    stats = HistoricalStore(cfg.data.dir).download(syms, start_d, end_d)
    typer.echo(f"done: {stats}")


@app.command()
def backtest(
    start: Optional[str] = typer.Option(None, help="First trading day YYYY-MM-DD"),
    end: Optional[str] = typer.Option(None, help="Last trading day YYYY-MM-DD"),
    days: int = typer.Option(29, help="If --start is omitted: number of days ending at --end"),
    symbols: Optional[str] = typer.Option(None, help="Comma-separated symbols (default: config universe)"),
    out: Optional[Path] = typer.Option(None, help="Output directory (default: reports/<timestamp>)"),
    config: Optional[Path] = ConfigOpt,
    verbose: bool = typer.Option(False, "-v"),
) -> None:
    """Backtest the strategy on cached history and write a report."""
    from autotr.data.historical import default_end_date
    from autotr.engine.backtest import run_backtest
    from autotr.report.metrics import compute_metrics, format_summary
    from autotr.report.plot import plot_equity
    from autotr.report.tradelog import write_trades_csv

    _setup_logging(verbose)
    cfg = load_config(config)
    end_d = _parse_date(end) if end else default_end_date()
    start_d = _parse_date(start) if start else end_d - timedelta(days=days - 1)
    syms = _symbols(symbols, cfg.universe.symbols)

    res = run_backtest(cfg, start_d, end_d, syms)
    ex = res.core.exchange
    m = compute_metrics(ex.trades, res.equity, cfg.account.starting_balance, ex.balance, res.start_ts, res.end_ts)

    out_dir = out or Path("reports") / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    write_trades_csv(ex.trades, out_dir / "trades.csv")
    plot_equity(res.equity, ex.trades, cfg.account.starting_balance, out_dir / "equity.png",
                title=f"autotr backtest {start_d} .. {end_d}  ({len(res.symbols)} symbols)")
    extra = {
        "signals": len(res.core.signals),
        "risk_rejections": dict(res.core.risk.rejections),
        "entries_filled": ex.stats.entries_filled,
        "entries_cancelled_at_fill": ex.stats.entries_cancelled,
        "liquidations": ex.stats.liquidations,
        "funding_source": res.funding_source,
        "exchange_filters": "exchangeInfo" if res.core.filters.has_real_data else "approximate defaults",
        "elapsed_s": round(res.elapsed_s, 1),
    }
    (out_dir / "summary.json").write_text(json.dumps({**m, **extra}, indent=2, default=str))
    (out_dir / "config.json").write_text(cfg.model_dump_json(indent=2))

    typer.echo("")
    typer.echo(format_summary(m))
    typer.echo(f"Signals           {extra['signals']}  (risk rejections: {extra['risk_rejections']}, "
               f"cancelled at fill: {extra['entries_cancelled_at_fill']}, liquidations: {extra['liquidations']})")
    typer.echo(f"Report            {out_dir}/  (summary.json, trades.csv, equity.png)  [{extra['elapsed_s']}s]")


if __name__ == "__main__":
    app()
