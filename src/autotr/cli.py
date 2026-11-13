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


@app.command("download-movers")
def download_movers(
    windows: str = typer.Option("scalp", help="Download for every window of this research window set"),
    top_n: int = typer.Option(20, help="Coins per day, by |daily change|"),
    min_volume: float = typer.Option(20_000_000, help="Minimum daily quote volume (USDT)"),
    start: Optional[str] = typer.Option(None, help="Instead of --windows: first day YYYY-MM-DD"),
    end: Optional[str] = typer.Option(None, help="Instead of --windows: last day YYYY-MM-DD"),
) -> None:
    """Download each day's top movers (incl. delisted coins) for `universe.source: movers` backtests."""
    from autotr.data.movers import MoversStore
    from autotr.research import WINDOW_SETS

    _setup_logging(False)
    cfg = load_config(None)
    if start and end:
        wins = [(_parse_date(start), _parse_date(end))]
    else:
        wins = [(w.start, w.end) for w in WINDOW_SETS[windows]]
    typer.echo(f"Selecting the top {top_n} daily movers (>= ${min_volume / 1e6:.0f}M volume) for "
               f"{', '.join(f'{a}..{b}' for a, b in wins)}")
    stats = MoversStore(cfg.data.dir).download_windows(wins, top_n, min_volume)
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
    syms = _symbols(symbols, None) if symbols else None  # None = config universe (list or movers)

    res = run_backtest(cfg, start_d, end_d, syms)
    ex = res.core.exchange
    m = compute_metrics(ex.trades, res.equity, cfg.account.starting_balance, ex.balance, res.start_ts, res.end_ts)

    out_dir = out or Path("reports") / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    write_trades_csv(ex.trades, out_dir / "trades.csv")
    plot_equity(res.equity, ex.trades, cfg.account.starting_balance, out_dir / "equity.png",
                title=f"autotr backtest {start_d} .. {end_d}  ({len(res.symbols)} symbols)")
    from autotr.report.visualize import plot_dashboard, plot_trade_gallery
    sc = cfg.strategy
    strat = (f"{sc.name} ({sc.entry_tf_minutes}m entries, {sc.structure_tf_minutes // 60 or sc.structure_tf_minutes}"
             f"{'h' if sc.structure_tf_minutes >= 60 else 'm'} structure)")
    label = f"Backtest {start_d} → {end_d}  ·  {len(res.symbols)} coins  ·  {strat}"
    plot_dashboard(ex.trades, res.equity, m, out_dir / "dashboard.png", label)
    plot_trade_gallery(cfg, ex.trades, out_dir / "trades.png", f"{label}  ·  best wins (top) and typical losses (bottom)")
    extra = {
        "signals": len(res.core.signals),
        "risk_rejections": dict(res.core.risk.rejections),
        "rungs_filled": ex.stats.rungs_filled,
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
    from autotr.optimize import monthly_returns
    months = monthly_returns(ex.trades, cfg.account.starting_balance)
    if months:
        typer.echo("Per month         " + "  ".join(f"{k}: {v:+.1f}%" for k, v in months.items()))
    typer.echo(f"Report            {out_dir}/  (summary.json, trades.csv, dashboard.png, trades.png)  [{extra['elapsed_s']}s]")


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
            from autotr.engine.backtest import backtest_symbols
            history = load_history(cfg, start_d, end_d, _symbols(symbols, None) if symbols
                                   else backtest_symbols(cfg, start_d, end_d))
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


@app.command()
def fast1s(
    db: Optional[Path] = typer.Option(None, help="SQLite file for the paper account"),
    reset: bool = typer.Option(False, help="Start a fresh paper account (deletes the existing database)"),
    config: Optional[Path] = ConfigOpt,
    verbose: bool = typer.Option(False, "-v"),
) -> None:
    """PROTOTYPE: paper-trade on 1-SECOND candles (Binance continuousKline_1s) instead of 1m.

    Live only (REST has no 1s history to replay). Drives the same scalp_ignition strategy at 1s
    cadence; 24h context comes from the REST ticker. View it with `autotr web -c <config>`."""
    import asyncio

    import httpx

    from autotr.data.rest import BinanceRest, save_exchange_info
    from autotr.engine.fast1s import Fast1sEngine, Fast1sFeed
    from autotr.report.tradelog import LiveStore
    from autotr.sim.filters import FilterBook

    _setup_logging(verbose)
    cfg = load_config(config)
    db_path = db or Path(cfg.live.db_path)
    if reset and db_path.exists():
        db_path.unlink()
    store = LiveStore(db_path)

    async def go() -> None:
        rest = BinanceRest(cfg.live.rest_url)
        try:
            try:
                save_exchange_info(await rest.exchange_info(), cfg.data.dir)
            except httpx.HTTPError as exc:
                typer.echo(f"Cannot reach the Binance futures API at {cfg.live.rest_url}:\n  {exc}", err=True)
                raise typer.Exit(1)
            engine = Fast1sEngine(cfg, store, FilterBook.load(cfg.data.dir))
            if not engine.resumed:
                typer.echo(f"New 1s paper account with ${cfg.account.starting_balance:.2f} -> {db_path}")
            feed = Fast1sFeed(cfg, rest, must_watch=lambda: set(engine.ex.positions) | set(engine.ex.pending))
            typer.echo("Prototype 1-second engine running (Ctrl+C to stop). Warming up ~1-2 min per mover…")
            await engine.run(feed)
        finally:
            await rest.close()

    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        typer.echo("\nstopped — account saved")
    finally:
        store.close()


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


@app.command()
def web(
    host: str = typer.Option("0.0.0.0", help="Address to listen on (127.0.0.1 = this machine only)"),
    port: int = typer.Option(8080, help="Port to listen on"),
    db: Optional[Path] = typer.Option(None, help="SQLite file of the paper account"),
    config: Optional[Path] = ConfigOpt,
) -> None:
    """Password-protected, read-only web dashboard of the paper account (runs next to `live`)."""
    from autotr.web import load_password, make_server

    _setup_logging(False)
    cfg = load_config(config)
    db_path = db or Path(cfg.live.db_path)
    password, pw_file = load_password(Path(cfg.data.dir))
    try:
        srv = make_server(host, port, db_path, cfg, password)
    except OSError as exc:
        typer.echo(f"Cannot listen on {host}:{port}: {exc.strerror}. Is the dashboard already running? "
                   "Pick another port with --port.", err=True)
        raise typer.Exit(1)
    typer.echo(f"Dashboard for {db_path} on http://{host}:{port}  (Ctrl+C to stop)")
    typer.echo("Login: any username, password "
               + (f"in {pw_file}: {password}" if pw_file else "from $AUTOTR_WEB_PASSWORD"))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        typer.echo("\nstopped")
    finally:
        srv.server_close()


@app.command()
def optimize(
    train_start: str = typer.Option(..., help="First training day YYYY-MM-DD"),
    train_end: str = typer.Option(..., help="Last training day YYYY-MM-DD"),
    test_start: str = typer.Option(..., help="First out-of-sample test day YYYY-MM-DD"),
    test_end: str = typer.Option(..., help="Last out-of-sample test day YYYY-MM-DD"),
    symbols: Optional[str] = typer.Option(None, help="Comma-separated symbols (default: config universe)"),
    workers: Optional[int] = typer.Option(None, help="Parallel backtests (default: CPU count)"),
    out: Path = typer.Option(Path("reports/optimize"), help="Output directory"),
    grid: str = typer.Option("", help="Settings grid: confluence | scalper (default: the configured strategy)"),
    config: Optional[Path] = ConfigOpt,
) -> None:
    """Grid-search settings on the training period; evaluate the best once on the test period."""
    import csv
    import time

    import yaml

    from autotr.engine.backtest import run_backtest
    from autotr.optimize import GRIDS, apply_params, describe, evaluate, run_grid, score
    from autotr.data.replay import load_history
    from autotr.report.metrics import compute_metrics
    from autotr.report.visualize import plot_dashboard, plot_trade_gallery

    _setup_logging(False)
    logging.getLogger("autotr").setLevel(logging.WARNING)
    cfg = load_config(config)
    grid_name = grid or cfg.strategy.name
    if grid_name not in GRIDS:
        raise typer.BadParameter(f"unknown grid {grid_name!r}; choose from {', '.join(GRIDS)}")
    DEFAULT_GRID = GRIDS[grid_name]
    tr0, tr1, te0, te1 = map(_parse_date, (train_start, train_end, test_start, test_end))
    if te0 <= tr1:
        raise typer.BadParameter("the test period must start after the training period")
    syms = _symbols(symbols, cfg.universe.symbols)
    out.mkdir(parents=True, exist_ok=True)
    train_days = (tr1 - tr0).days + 1
    min_trades = max(int(train_days * 0.5), 10)

    n_points = 1
    for v in DEFAULT_GRID.values():
        n_points *= len(v)
    typer.echo(f"Training {tr0} .. {tr1} ({train_days} days, {len(syms)} coins): {n_points} settings")
    t0 = time.time()

    best_so_far: list = []

    def progress(r, i, n):
        m = r.metrics
        if not best_so_far or score(r, min_trades) > score(best_so_far[0], min_trades):
            best_so_far[:] = [r]
        elapsed = time.time() - t0
        eta = elapsed / i * (n - i)
        b = best_so_far[0].metrics
        typer.echo(f"  [{i:3d}/{n}  {elapsed / 60:4.1f}m, ~{eta / 60:4.1f}m left]  {m['return_pct']:+6.1f}%  "
                   f"{m['trades']:4d} trades  PF {m['profit_factor']:4.2f}  | {describe(r.params)}"
                   f"   (best so far {b['return_pct']:+.1f}%)")

    results = run_grid(cfg, tr0, tr1, syms, grid=DEFAULT_GRID, workers=workers, on_result=progress)
    results.sort(key=lambda r: score(r, min_trades), reverse=True)

    with open(out / "grid_train.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["settings", "return_pct", "trades", "win_rate", "profit_factor", "max_drawdown_pct",
                    "fees", "worst_month_pct"])
        for r in results:
            m = r.metrics
            w.writerow([describe(r.params), round(m["return_pct"], 2), m["trades"], round(m["win_rate"], 3),
                        round(m["profit_factor"], 3), round(m["max_drawdown_pct"], 2), round(m["fees"], 2),
                        round(r.worst_month, 2)])

    typer.echo(f"\nTop 10 on the training period (min {min_trades} trades):")
    typer.echo(f"  {'return':>8} {'trades':>6} {'win':>5} {'PF':>5} {'maxDD':>6} {'worst mo':>8}  settings")
    for r in results[:10]:
        m = r.metrics
        typer.echo(f"  {m['return_pct']:+7.1f}% {m['trades']:6d} {m['win_rate']:5.0%} {m['profit_factor']:5.2f} "
                   f"{m['max_drawdown_pct']:5.1f}% {r.worst_month:+7.1f}%  {describe(r.params)}")

    best = results[0]
    baseline_params = {k: (getattr(cfg.strategy, "rsi_oversold"), getattr(cfg.strategy, "rsi_overbought"))
                       if k.endswith("rsi_levels") else getattr(cfg.strategy, k.split(".")[1]) for k in DEFAULT_GRID}

    typer.echo(f"\nOut-of-sample test {te0} .. {te1} (never seen by the optimizer):")
    test_hist = load_history(cfg, te0, te1, syms)
    train_hist = load_history(cfg, tr0, tr1, syms)
    rows = []
    for label, params in (("baseline", baseline_params), ("optimized", best.params)):
        tr = evaluate(cfg, params, tr0, tr1, train_hist)
        c = apply_params(cfg, params)
        res = run_backtest(c, te0, te1, progress=False, history=test_hist)
        ex = res.core.exchange
        m = compute_metrics(ex.trades, res.equity, c.account.starting_balance, ex.balance, res.start_ts, res.end_ts)
        rows.append((label, params, tr.metrics, m))
        d = out / f"test_{label}"
        d.mkdir(exist_ok=True)
        title = f"{label.capitalize()} settings · out-of-sample {te0} → {te1} · {len(res.symbols)} coins"
        plot_dashboard(ex.trades, res.equity, m, d / "dashboard.png", title)
        plot_trade_gallery(c, ex.trades, d / "trades.png", title + " · best wins (top) and typical losses (bottom)")
        (d / "summary.json").write_text(json.dumps(m, indent=2, default=str))

    typer.echo(f"  {'':10} {'train return':>13} {'test return':>12} {'test trades':>12} {'test win':>9} "
               f"{'test PF':>8} {'test maxDD':>11} {'test fees':>10}")
    for label, params, trm, m in rows:
        typer.echo(f"  {label:10} {trm['return_pct']:+12.1f}% {m['return_pct']:+11.1f}% {m['trades']:12d} "
                   f"{m['win_rate']:9.0%} {m['profit_factor']:8.2f} {m['max_drawdown_pct']:10.1f}% {m['fees']:10.2f}")
    typer.echo(f"\nOptimized settings: {describe(best.params)}")

    best_cfg = apply_params(cfg, best.params)
    keys = sorted({k.split(".")[1] for k in DEFAULT_GRID} - {"rsi_levels"} | {"rsi_oversold", "rsi_overbought"})
    (out / "optimized.yaml").write_text(
        f"# Best of {n_points} settings on {tr0}..{tr1}; out-of-sample {te0}..{te1}: "
        f"{rows[1][3]['return_pct']:+.1f}%\n"
        + yaml.safe_dump({"strategy": {k: getattr(best_cfg.strategy, k) for k in keys}}, sort_keys=True))
    typer.echo(f"Saved {out}/optimized.yaml (use with --config), grid_train.csv, test_baseline/, test_optimized/")


@app.command()
def ablation(
    start: str = typer.Option(..., help="First day YYYY-MM-DD"),
    end: str = typer.Option(..., help="Last day YYYY-MM-DD"),
    symbols: Optional[str] = typer.Option(None, help="Comma-separated symbols (default: config universe)"),
    workers: Optional[int] = typer.Option(None),
    out: Path = typer.Option(Path("reports/ablation")),
    config: Optional[Path] = ConfigOpt,
) -> None:
    """Switch each part of the strategy off in turn to see what it contributes."""
    import time

    from autotr.optimize import ABLATION, run_grid

    _setup_logging(False)
    logging.getLogger("autotr").setLevel(logging.WARNING)
    cfg = load_config(config)
    d0, d1 = _parse_date(start), _parse_date(end)
    syms = _symbols(symbols, cfg.universe.symbols)
    labels = {i: name for i, (name, _) in enumerate(ABLATION)}
    points = [dict(params, **{"_id": i}) for i, (_, params) in enumerate(ABLATION)]
    t0 = time.time()
    typer.echo(f"Ablation {d0} .. {d1}: {len(points)} variants, {len(syms)} coins")

    def progress(r, i, n):
        m = r.metrics
        typer.echo(f"  [{i}/{n} {time.time() - t0:4.0f}s] {labels[r.params['_id']]:42s} "
                   f"{m['return_pct']:+7.1f}%  {m['trades']:4d} trades  avg {m['expectancy_r']:+.3f}R")

    results = run_grid(cfg, d0, d1, syms, workers=workers, on_result=progress, points=points)
    results.sort(key=lambda r: r.params["_id"])
    out.mkdir(parents=True, exist_ok=True)
    months = sorted({k for r in results for k in r.monthly})
    hdr = f"  {'variant':42s} {'return':>8} {'trades':>6} {'win':>5} {'avg R':>7} {'PF':>5} {'maxDD':>6} {'fees':>7}  " + " ".join(f"{m[2:]:>7}" for m in months)
    lines = [hdr]
    for r in results:
        m = r.metrics
        lines.append(f"  {labels[r.params['_id']]:42s} {m['return_pct']:+7.1f}% {m['trades']:6d} {m['win_rate']:5.0%} "
                     f"{m['expectancy_r']:+7.3f} {m['profit_factor']:5.2f} {m['max_drawdown_pct']:5.1f}% {m['fees']:7.2f}  "
                     + " ".join(f"{r.monthly.get(mo, 0.0):+6.1f}%" for mo in months))
    typer.echo("\n" + "\n".join(lines))
    (out / "ablation.txt").write_text("\n".join(lines) + "\n")
    typer.echo(f"\nSaved {out}/ablation.txt")


@app.command("research-eval")
def research_eval(
    config: Path = typer.Option(..., "--config", help="Strategy config (YAML)"),
    out: Optional[Path] = typer.Option(None, help="Output dir (default: reports/research/<config stem>)"),
    holdout: bool = typer.Option(False, help="Also run the locked holdout windows (final candidates only)"),
    only_holdout: bool = typer.Option(False, help="Run only the holdout windows"),
    workers: int = typer.Option(4, help="Windows evaluated in parallel"),
) -> None:
    """Evaluate a strategy config on the fixed research / validation (/ holdout) windows."""
    import time

    from autotr.research import evaluate_windows, save, select_windows, summary_table

    _setup_logging(False)
    logging.getLogger("autotr").setLevel(logging.ERROR)
    cfg = load_config(config)
    kinds = {"holdout"} if only_holdout else {"research", "validation"} | ({"holdout"} if holdout else set())
    out = out or Path("reports/research") / config.stem
    t0 = time.time()
    results = evaluate_windows(cfg, select_windows(kinds, cfg.research.windows), out, workers)
    save(out, config.stem, str(config), results)
    typer.echo(summary_table(f"{config.stem}  ({time.time() - t0:.0f}s)", results))
    typer.echo(f"Saved {out}/results.json, summary.txt, trades_R*.csv")


if __name__ == "__main__":
    app()
