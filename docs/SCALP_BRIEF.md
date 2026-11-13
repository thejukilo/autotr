# Scalping search brief (Nov 2026)

Goal: find the best **scalping** strategy (decisions on 1m–15m candles, holds of minutes to a few
hours) for **Binance USDⓈ-M perpetuals on the day's top movers**: the coins with the biggest 24h
change (e.g. a coin up +70% today), which is what the owner trades manually. Same honest process as
the momentum search (`docs/RESEARCH_LOG.md`): research windows to design, validation windows to
accept/reject, a locked holdout run once for pre-registered finalists.

Project: `/workspaces/workspaces` (Python). Run commands as
`export PATH="$HOME/.local/bin:$PATH"; uv run autotr ...`. The machine has **4 CPU cores** shared by
all agents; use the `--workers` value you are given.

## Data and windows
- Universe = each day's **top 20 USDT perpetuals by |daily change|** with ≥ $20M daily volume,
  delisted coins included (no survivorship bias). 1m candles + funding were downloaded for each
  selected coin from 2 days before to 1 day after each day it was selected (`autotr download-movers`,
  index in `data/movers/index.json`). Coins outside their downloaded days are invisible to the scanner.
- At run time the scanner picks the live-style top movers among them (`rank_by: change_24h`,
  `top_n`, `min_quote_volume_24h`), rescanned every `rescan_minutes`.
- Windows (`research.windows: scalp`, see `src/autotr/research.py`):
  | Name | Kind | Period |
  |---|---|---|
  | SR1, SR2 | research (full diagnostics + trade CSVs) | 2025-09-01..21, 2026-02-02..22 |
  | SV1, SV2 | validation (summary numbers only) | 2025-12-01..21, 2026-05-04..24 |
  | SH1, SH2 | **holdout: locked**, final candidates only, run once | 2026-08-17..09-06, 2026-10-19..11-08 |
- Only ~2 days of history exist before a coin becomes a mover: keep
  `structure_history_bars × structure_tf_minutes` ≤ ~1 day (e.g. 288 × 5m, 96 × 15m, 24 × 60m).

## Costs (the owner's real fees: 0.05% taker / 0.02% maker)
- Market entries and stops pay **taker 0.05% + 5 bps slippage**; limit entries (ladder rungs) and
  take-profits pay **maker 0.02%** and only fill when price trades *through* the limit.
- Round trip: market in + stop out ≈ 0.20%; limit in + limit TP out ≈ 0.04%. With scalping-size
  targets this decides everything; previous 1m/5m strategies lost 60–90% mostly to fees.
- 1m OHLC only: no order book, no trades, no sub-minute ordering. When stop and target are both in
  one candle, the stop is assumed first (pessimistic). Funding is charged at real rates (movers can
  have extreme funding).

## Strategy interface
A research strategy is `src/autotr/strategy/research/<name>.py` with class `Strategy(cfg, fees)`:
- `on_bar(st) -> Signal | None` is called on every closed **entry-timeframe** candle for symbols the
  scanner currently selects. `st` is a `SymbolState` (`src/autotr/market/features.py`):
  `st.bars_e` (recent entry-TF `Bar`s, length `entry_history_bars`), `st.t_s/h_s/l_s/c_s` (structure-TF
  history), `st.last_bar`, `st.change_24h`, `st.quote_volume_24h`, `st.rsi_now`, `st.atr_e.value`, …
- optional `observe(st)`: called for **every** symbol on each entry candle (cross-sectional ideas).
- optional `manage(st, pos) -> new_stop | None`: called on each entry candle of an open position
  (trailing / break-even stops; stops can only tighten).
- Build signals with `autotr.strategy.research._common.signal(symbol, side, ts, price, stop, tp, reason)`
  (helpers there: ema, sma, stdev, atr, rsi, adx, efficiency_ratio, chandelier_stop). Set
  `Signal.ladder = [(price, weight), ...]` (nearest first, weights sum to 1) for **limit entries**;
  unfilled rungs expire after `strategy.ladder_expiry_bars` entry candles. Without a ladder the
  entry is a market order at the next candle's open.
- Exits: stop (taker), take-profit (maker limit), `time_stop_minutes`, plus `manage`.
- Existing examples: `research/xs_momentum_trail.py`, `research/bb_reversion.py`, `scalper.py`,
  `confluence.py`. Risk sizing is in `src/autotr/risk/manager.py`.

## Config template (`config/research/scalp/<name>.yaml`)
Every scalping config starts with this common block (keep it identical across strategies so results
are comparable; only change it as an explicit, documented experiment):
```yaml
universe: {source: movers}
research: {windows: scalp}
exchange: {taker_fee: 0.0005, maker_fee: 0.0002, slippage_bps: 5.0}
scanner: {rank_by: change_24h, top_n: 8, min_quote_volume_24h: 50000000, rescan_minutes: 15}
risk: {risk_per_trade: 0.01, max_positions: 3, max_trades_per_day: 40, daily_loss_limit: 0.06}
strategy:
  name: scalp_<name>
  entry_tf_minutes: 1          # 1, 3, 5 or 15
  structure_tf_minutes: 5
  structure_history_bars: 288
  entry_history_bars: 120
  time_stop_minutes: 120
  cooldown_minutes: 15
  ladder_expiry_bars: 5
  params: {...}
```
Evaluate: `uv run autotr research-eval --config config/research/scalp/<name>.yaml --workers <N>`
→ `reports/research/<name>/` (`results.json`, `summary.txt`, `trades_SR1.csv`, `trades_SR2.csv`).
Quick smoke test: `uv run autotr backtest --config <cfg> --start 2025-09-01 --end 2025-09-02 --out /tmp/smoke_<name>`.

## Rules
- **Creator** writes strategies/configs and keeps the log `docs/SCALP_RESEARCH_LOG.md`. Don't touch
  engine code (`src/autotr/{engine,sim,data,market,risk}`) without stating why in the log; never
  change the cost model to make a strategy look better.
- **Testers** don't edit `src/` or `config/`. Report crashes with the traceback.
- Validation windows: summary numbers only, never tune on their trades. **Holdout only when the task
  explicitly says so**, for finalists written into the log *before* the run.
- Judge on profit factor, avg R and consistency across windows, not peak return. PF 1.05 over 100
  trades is noise. Report fees vs gross PnL for every strategy.
