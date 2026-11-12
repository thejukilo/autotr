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


@app.command("fetch-exchange-info")
def fetch_exchange_info(config: Optional[Path] = ConfigOpt) -> None:
    """Download real symbol filters (tick/lot size, min notional) from Binance into the data dir."""
    import asyncio

    from autotr.data.rest import BinanceRest, save_exchange_info, usdt_perpetuals

    cfg = load_config(config)

    async def go() -> None:
        rest = BinanceRest(cfg.live.rest_url)
        try:
            info = await rest.exchange_info()
        finally:
            await rest.close()
        path = save_exchange_info(info, cfg.data.dir)
        typer.echo(f"saved {len(usdt_perpetuals(info))} USDT perpetuals to {path}")

    asyncio.run(go())


@app.command()
def live(
    replay: bool = typer.Option(False, help="Replay cached history through the live engine instead of Binance"),
    start: Optional[str] = typer.Option(None, help="--replay: first day YYYY-MM-DD"),
    end: Optional[str] = typer.Option(None, help="--replay: last day YYYY-MM-DD"),
    speed: float = typer.Option(0.0, help="--replay: 60 = one 1m bar per second, 0 = max speed"),
    symbols: Optional[str] = typer.Option(None, help="--replay: comma-separated symbols"),
    db: Optional[Path] = typer.Option(None, help="SQLite file for the paper account"),
    reset: bool = typer.Option(False, help="Start a fresh paper account (deletes the existing database)"),
    config: Optional[Path] = ConfigOpt,
    verbose: bool = typer.Option(False, "-v"),
) -> None:
    """Run the paper-trading bot on live Binance data (or --replay history). Ctrl+C to stop;
    the account is saved and resumes on the next start."""
    import asyncio

    from autotr.engine.live import LiveEngine
    from autotr.report.tradelog import LiveStore
    from autotr.sim.filters import FilterBook

    _setup_logging(verbose)
    cfg = load_config(config)
    db_path = db or Path(cfg.live.db_path if not replay else Path(cfg.data.dir) / "replay.db")
    if (reset or replay) and db_path.exists():
        db_path.unlink()  # replays always start from a clean account
    store = LiveStore(db_path)

    async def go() -> None:
        if replay:
            from autotr.data.historical import default_end_date
            from autotr.data.replay import ReplayFeed, load_history

            end_d = _parse_date(end) if end else default_end_date()
            start_d = _parse_date(start) if start else end_d - timedelta(days=2)
            history = load_history(cfg, start_d, end_d, _symbols(symbols, cfg.universe.symbols))
            feed = ReplayFeed(history, speed)
            # fills are still persisted immediately; periodic snapshots once per simulated hour
            engine = LiveEngine(cfg, store, FilterBook.load(cfg.data.dir), bar_fills=True,
                                save_every_ms=60 * 60_000)
            typer.echo(f"Replaying {start_d} .. {end_d} for {len(history.symbols)} symbols into {db_path}")
            await engine.run(feed)
            return

        from autotr.data.live import BinanceFeed
        from autotr.data.rest import BinanceRest, save_exchange_info

        import httpx

        rest = BinanceRest(cfg.live.rest_url)
        try:
            try:
                save_exchange_info(await rest.exchange_info(), cfg.data.dir)
            except httpx.HTTPError as exc:
                typer.echo(f"Cannot reach the Binance futures API at {cfg.live.rest_url}:\n  {exc}\n"
                           "Check your internet connection. Some networks (e.g. corporate) and countries "
                           "block Binance; the live bot must run where Binance futures is reachable.", err=True)
                raise typer.Exit(1)
            engine = LiveEngine(cfg, store, FilterBook.load(cfg.data.dir), bar_fills=False)
            if not engine.resumed:
                typer.echo(f"New paper account with ${cfg.account.starting_balance:.2f} -> {db_path}")
            feed = BinanceFeed(cfg, rest, must_watch=lambda: set(engine.ex.positions) | set(engine.ex.pending))
            await engine.run(feed)
        finally:
            await rest.close()

    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        typer.echo("\nstopped — account saved")
    finally:
        store.close()
    if replay:
        _print_status(db_path, cfg)


def _print_status(db_path: Path, cfg) -> None:
    from autotr.report.metrics import compute_metrics, format_summary
    from autotr.report.tradelog import LiveStore

    store = LiveStore(db_path)
    try:
        trades, equity, state = store.trades(), store.equity(), store.load_state("engine")
    finally:
        store.close()
    if state is None:
        typer.echo("no account yet")
        return
    ex = state["exchange"]
    start_ts = equity[0].ts if equity else (trades[0].entry_ts if trades else 0)
    end_ts = equity[-1].ts if equity else (trades[-1].exit_ts if trades else start_ts + 1)
    # Open positions have already paid their entry fee / funding out of the balance; add it back so
    # the closed-trade reconciliation holds.
    realised = ex["balance"] + sum(p["entry_fee"] - p["funding"] for p in ex["positions"])
    m = compute_metrics(trades, equity, cfg.account.starting_balance, realised, start_ts, max(end_ts, start_ts + 1))
    typer.echo(format_summary(m))
    for p in ex["positions"]:
        typer.echo(f"Open              {p['side']} {p['symbol']} qty {p['qty']} @ {p['entry_price']} "
                   f"stop {p['stop']} tp {p['take_profit']}")


@app.command()
def status(db: Optional[Path] = typer.Option(None), config: Optional[Path] = ConfigOpt) -> None:
    """Show the paper account: balance, performance and open positions."""
    cfg = load_config(config)
    _print_status(db or Path(cfg.live.db_path), cfg)


if __name__ == "__main__":
    app()
