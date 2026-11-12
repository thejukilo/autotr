# autotr

A **simulated** scalping bot for **Binance USDⓈ-M perpetual futures**. It reads real Binance market
data, but every order is executed by a built-in paper exchange. **No API key is needed and no real
money is used.**

- Trades only the most volatile, liquid USDT perpetuals (rescanned every 15 min)
- Entry signals on **1m** candles; support/resistance and trendlines on **5m**
- Paper account starts at **$100**, **5x isolated leverage**, **2% risk per trade**
- Realistic simulation: taker/maker fees, slippage, funding payments, liquidation price, lot size and
  minimum-notional rules
- The same strategy code runs in **backtests**, **historical replays** and **live paper trading**

> ⚠️ This is an experiment, not financial advice. The first backtests are **unprofitable** (see
> [Current results](#current-results)). Don't trade real money with it.

## Setup (on your own computer)

Requirements: internet access to Binance (some countries and corporate networks block it).

1. Install **uv** (it installs Python for you):
   - macOS / Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
   - Windows (PowerShell): `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
2. Get the code and install the dependencies:
   ```bash
   git clone https://github.com/thejukilo/autotr.git
   cd autotr
   uv sync
   ```
3. Check that it works: `uv run pytest`

## Usage

```bash
# 1. Download history (1m candles + funding) from data.binance.vision — about 60 MB for 30 days
uv run autotr download --days 30

# 2. Backtest. Writes reports/<timestamp>/summary.json, trades.csv and equity.png
uv run autotr backtest --days 29

# 3. Replay recent history through the *live* engine (quick end-to-end check, no Binance API needed)
uv run autotr live --replay --start 2026-11-08 --end 2026-11-10

# 4. Live paper trading on real-time Binance data (Ctrl+C to stop, it resumes where it left off)
uv run autotr live
uv run autotr status          # balance, performance and open positions (in another terminal)
uv run autotr live --reset    # throw away the paper account and start again with $100
```

Add `-v` to `backtest` or `live` for debug logs (every signal, scanner changes).

### Configuration

All settings live in [`config/default.yaml`](config/default.yaml): fees, leverage, risk, scanner and
every strategy threshold. To change something, create `config/local.yaml` containing only the values
you want to override, and pass it to any command:

```yaml
# config/local.yaml
risk:
  risk_per_trade: 0.01
strategy:
  rsi_oversold: 25
  rsi_overbought: 75
```

```bash
uv run autotr backtest --days 29 --config config/local.yaml
```

**Check the fees.** The defaults are Binance's standard 0.05% taker / 0.02% maker. If you have a VIP
tier or a BNB discount, set your own rates. Fees are the single biggest cost of scalping.

## How it works

```
Binance WebSocket ─┐                        ┌─ Scanner: top 8 by 5m volatility, ≥ $50M 24h volume
 (or history file) │   1m candle closes     │
                   ├──────────────────────► Features per symbol: RSI(1m), ATR(1m/5m), 5m pivots,
 best bid/ask ─────┤                        │   S/R zones, trendlines
                   │                        ├─ Strategy → signal (entry / stop / target)
                   │                        ├─ Risk manager → position size or rejection
                   └──────────────────────► Paper exchange: fills, stops, targets, fees, funding,
                                                liquidation → trade log (SQLite / CSV)
```

**Strategy (`src/autotr/strategy/scalper.py`)**: mean reversion at levels.
- **Long** when 1m RSI crosses back **above 30** and, within the last 3 candles, price touched a
  **support zone** (a cluster of at least 2 swing highs/lows on 5m) or a **rising trendline** through
  5m swing lows, without breaking through it.
- **Short** is the mirror image: RSI crosses back **below 70** at resistance or a falling trendline.
- **Stop** just beyond the level (0.5 × ATR(1m)), never closer than 0.15% or wider than 3%.
- **Target** 2R, or the next opposing zone if that's closer. Trades whose reward:risk **after fees** is
  below 1.2 are skipped. **Time stop** after 30 minutes.

**Risk (`src/autotr/risk/manager.py`)**
- Size = 2% of equity ÷ (stop distance + fees + slippage), capped at 50% of equity as margin per
  position. So with tight stops the real risk is often below 2%; the actual risk is logged per trade.
- At most 2 positions at once, a 5-minute cooldown per coin, at most 40 trades a day, and trading
  pauses until 00:00 UTC after a **6% daily loss**.

**Simulation (`src/autotr/sim/exchange.py`)**
- Market entries and stops pay the taker fee plus slippage; take-profits are limit orders that pay
  the maker fee.
- Backtests use 1m candles. When the stop and the target both fall inside one candle, the stop is
  assumed to hit first (pessimistic). Live mode uses the real best bid/ask instead.
- Funding is charged at the real funding times (from historical data or the live mark-price stream).
- Isolated-margin liquidation price with a maintenance-margin rate. A trade is rejected if its stop
  would sit beyond the liquidation price.
- Real tick size, lot step and minimum notional are fetched from Binance in live mode (or with
  `autotr fetch-exchange-info`). Without them, approximate defaults are used.

## Current results

Baseline backtest, **2026-10-12 → 2026-11-10** (30 days, 34 coins, default settings):

| | |
|---|---|
| Balance | $100.00 → **$42.52 (−57.5%)** |
| Trades | 173 (5.8/day), win rate 30.6%, profit factor 0.48 |
| Gross PnL / fees | −$31.37 / **−$26.10** |
| Max drawdown | 57.7% |

Two lessons: the raw signal has **no edge** in this form, and **fees roughly double the loss**. Each
round trip costs about 0.1% of position size, which is a large share of a $1–2 risk per trade. Ideas
to explore next:

- Only trade **with the 5m/15m trend** (longs lost the most here)
- Require **confluence** (zone + trendline) and stronger levels (≥ 3 touches)
- Fewer but better trades: wider targets, a longer time stop, or 5m entry signals
- Limit-order entries (maker fee) instead of market orders
- Always compare across several separate periods to avoid overfitting one month

## Project layout

```
config/default.yaml      all settings
src/autotr/
  data/                  historical downloader, Binance REST + WebSocket feed, replay
  indicators/            RSI, ATR, pivots, S/R zones, trendlines
  market/                1m→5m aggregation, per-symbol feature state, volatility scanner
  strategy/              scalper strategy
  risk/                  position sizing and account guards
  sim/                   paper exchange and symbol filters
  engine/                shared core, backtest engine, live engine
  report/                metrics, trade log (CSV/SQLite), equity chart
  cli.py                 `autotr` command
tests/                   unit tests + backtest/replay/restart equivalence + fake-Binance feed test
```
