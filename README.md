# autotr

A **simulated** scalping bot for **Binance USDⓈ-M perpetual futures**. It reads real Binance market
data, but every order is executed by a built-in paper exchange. **No API key is needed and no real
money is used.**

- Trades the coins with the **biggest 24h moves** among liquid USDT perpetuals (rescanned every 15 min)
- **Confluence strategy** modelled on a manual method: RSI extreme + support/resistance, channels,
  trendlines and Elliott wave-5 fades, scored together; signals on **5m**, structure on **1h**
- **Laddered limit entries** (3 orders, 20/30/50%), stop beyond the structure, target at the opposite level
- Paper account starts at **$100**, **5x isolated leverage**, **2% risk per trade**
- Realistic simulation: taker/maker fees, slippage, funding payments, liquidation price, lot size and
  minimum-notional rules
- The same strategy code runs in **backtests**, **historical replays** and **live paper trading**

> ⚠️ This is an experiment, not financial advice. The original strategies are **unprofitable**; a
> strategy search found one small, not-yet-confirmed edge (see [Current results](#current-results)).
> Don't trade real money with it.

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
# 1. Download history (1m candles + funding) from data.binance.vision (~2 MB per coin per day).
#    The strategy needs ~11 days of warm-up before the first backtest day.
uv run autotr download --days 45

# 2. Backtest. Writes reports/<timestamp>/summary.json, trades.csv, dashboard.png and trades.png
uv run autotr backtest --days 29
uv run autotr ablation --start 2026-07-26 --end 2026-11-10   # what each strategy component adds

# 3. Replay recent history through the *live* engine (quick end-to-end check, no Binance API needed)
uv run autotr live --replay --start 2026-11-08 --end 2026-11-10

# 4. Live paper trading on real-time Binance data (Ctrl+C to stop, it resumes where it left off)
uv run autotr live
uv run autotr status          # balance, performance and open positions (in another terminal)
uv run autotr live --reset    # throw away the paper account and start again with $100
```

Add `-v` to `backtest` or `live` for debug logs (every signal, scanner changes).

### Strategy research

```bash
# Download the full research history (Jul 2025 → Nov 2026, ~1 GB for 34 coins)
uv run autotr download --end 2026-11-10 --days 498

# Evaluate a strategy on the fixed research + validation windows (holdout only for final candidates)
uv run autotr research-eval --config config/research/xs_mom_trail_a.yaml
uv run autotr research-eval --config config/research/xs_mom_trail_a.yaml --holdout

# Grid-search the confluence strategy with a train/test split
uv run autotr optimize --grid confluence --train-start 2026-07-26 --train-end 2026-09-30 \
    --test-start 2026-10-01 --test-end 2026-11-10
```

Research strategies live in `src/autotr/strategy/research/<name>.py` (a `Strategy` class with
`on_bar`, plus optional `observe` to see every coin and `manage` to trail the stop) and are selected
with `strategy.name` in a config under `config/research/`. Progress of every backtest window is
appended to `reports/research/progress.log`. The full research log is in
[docs/RESEARCH_LOG.md](docs/RESEARCH_LOG.md).

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

**Strategy (`src/autotr/strategy/confluence.py`)** — the manual method, scored:
1. **Coins**: the 8 biggest 24h movers with at least $50M of 24h volume.
2. **RSI gate** (5m): oversold (≤30) for longs / overbought (≥70) for shorts within the last 3 candles.
3. **Score** on 1h structure: RSI (+1), RSI ≤20/≥80 (+0.5), RSI turning back (+0.5), at an S/R zone
   (+1, +0.5 if touched ≥3 times), at a channel edge (+1), at a trendline (+1), completed Elliott
   5-wave impulse to fade (+1.5). A trade needs **≥3 points**.
4. **Ladder**: three limit orders from just inside the price to the level, 20% / 30% / 50%
   (biggest closest to the level). Unfilled orders cancel after 30 minutes.
5. **Stop** beyond the structure the setup relies on (zone, channel edge, trendline or the wave-5
   extreme). **Target** at the first opposing zone / channel edge / wave-4 area that pays at least
   1.5× the risk after fees. Time stop 12h.

Every component can be switched off in the config (`use_zones`, `use_channels`, `use_trendlines`,
`use_elliott`, `use_ladder`); `uv run autotr ablation --start … --end …` measures what each one
contributes. The original 1m scalper is still available: `--config config/scalper.yaml`.

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

### Strategy search (Nov 2026)

Fifteen strategies were generated and tested by a creator agent and tester agents, on 34 coins with
realistic fees, using separate data windows so that ideas are not tuned on the data they are judged on:

| Window | Use | Period |
|---|---|---|
| R1, R2 | research: strategies are designed and diagnosed on these | Aug–Oct 2025, Jan–Mar 2026 |
| V1, V2 | validation: accept / reject only | Nov–Dec 2025, Apr–Jun 2026 |
| H1, H2 | holdout: locked until the final candidate | Jul–Sep 2026, Sep–Nov 2026 |

Research strategies risk 1% per trade with up to 4 positions. Profit factor (PF) = gross wins / gross losses.

| Strategy | R1 | R2 | V1 | V2 | Verdict |
|---|---|---|---|---|---|
| **xs_mom_trail_a**: 4h cross-sectional momentum + trailing stop | **+6.4%** PF 1.21 | **+10.0%** PF 1.28 | **+3.7%** PF 1.23 | **+6.0%** PF 1.19 | **candidate** (robustness + holdout checks running) |
| xs_momentum: same entries, fixed 3R target | +4.2% PF 1.08 | +6.5% PF 1.11 | −3.8% PF 0.89 | +0.7% PF 1.02 | marginal |
| donchian_trail: 4h breakout + trailing stop | +0.9% | −2.9% | −10.3% | +0.6% | break-even |
| donchian_trend: 1h breakout, 2.5R | −9.8% | +17.9% | −30.7% | −27.4% | dead |
| bb_reversion: 1h Bollinger fade (ADX < 22) | −9.0% | −10.2% | +17.3% | +22.1% | regime-dependent |
| bb_reversion_v2: re-entry, wider stop | −16.0% | −49.7% | +3.4% | +15.1% | dead |
| exhaustion_fade: 24h mover reversal | −3.0% | −7.2% | +1.3% | +7.1% | marginal |
| squeeze_breakout: 1h Bollinger squeeze | −18.6% | −35.6% | −38.9% | −31.1% | dead |
| trend_pullback: 4h trend, 15m RSI dip | −89.3% | −82.8% | −58.8% | −72.5% | dead |

**xs_mom_trail_a** ranks all coins every 4 hours by their 7-day return, goes long the 3 strongest
(if above their 50-bar EMA) and short the 3 weakest (if below), with a 3 ATR stop that trails
3 ATR behind the best close and no fixed target. It trades about once a day.

Conclusions so far:
- **Low-timeframe trading is killed by fees.** Strategies on 1m–15m entries trade 5–13 times a day
  and lose 60–90% even when they are roughly break-even before fees.
- **Short holds lose, long holds win.** Across every strategy, trades closed within a few hours
  lost money and trades held 12–48h made money: tight stops get hit by noise before the idea plays out.
- **Fading big moves doesn't work reliably** (scalper, confluence, exhaustion fade, Bollinger fade):
  it only paid off in the choppier validation windows.
- **Relative-strength momentum is the only idea with an edge in every window**, and only once
  winners were allowed to run with a trailing stop. The edge is small (≈ +0.08R per trade), so it
  still has to survive parameter-robustness checks, higher fees and the locked holdout windows.
- Caveats: all four research/validation windows are bear markets (median coin −18% to −37%); the
  coin list is today's (survivorship bias); results are simulations.

### Original strategies

All backtests below use 34 coins, $100 start, 5x leverage, 2% risk, realistic fees. **None is
profitable.**

**Confluence grid search** (`autotr optimize --grid confluence`): best of 24 settings on
Jul 26 – Sep 30 (15m entries, score ≥ 4, 60/40 ladder, wait for the level to hold) made +3.2% in
training but **−26.3% on the unseen Oct 1 – Nov 10** test (default settings: −55.4%).

**Ablation, 2026-07-26 → 2026-11-10 (108 days)** — `reports/ablation/ablation.txt`:

| Variant | Return | Trades | Win rate | Profit factor | Fees |
|---|---|---|---|---|---|
| Full confluence method | −84.4% | 849 | 33% | 0.75 | $44.38 |
| …without ladder (single market entry) | −95.7% | 720 | 26% | 0.65 | $49.28 |
| …without S/R zones | −47.4% | 316 | 34% | 0.75 | $25.56 |
| …without channels | −82.6% | 836 | 35% | 0.75 | $43.64 |
| …without trendlines | −87.2% | 818 | 34% | 0.71 | $39.82 |
| …without Elliott | −81.5% | 834 | 34% | 0.77 | $47.95 |
| …scanner by volatility instead of 24h change | −79.6% | 907 | 36% | 0.79 | $45.58 |
| Old 1m scalper | −92.2% | 716 | 35% | 0.65 | $64.03 |

What the data says:
- **Laddering helps** — clearly better than a single market entry (profit factor 0.75 vs 0.65).
- **Trades that fill all 3 rungs lose** (21% win rate, the bulk of the losses): when price runs
  through the whole ladder the level is usually breaking. Trades that fill only the first rung win
  58% of the time.
- **No single component makes it profitable**; channels and Elliott fades slightly hurt, trendlines
  slightly help. Before fees the method is roughly break-even, so fees decide the result.

## Project layout

```
config/default.yaml      all settings
config/research/         configs of the research strategies (+ robust/ variations)
src/autotr/
  data/                  historical downloader, Binance REST + WebSocket feed, replay
  indicators/            RSI, ATR, pivots, S/R zones, trendlines, channels, Elliott waves
  market/                1m→5m aggregation, per-symbol feature state, volatility scanner
  strategy/              confluence strategy (manual method) and the old scalper
  strategy/research/     strategies from the automated strategy search
  research.py            fixed research / validation / holdout windows and diagnostics
  risk/                  position sizing and account guards
  sim/                   paper exchange and symbol filters
  engine/                shared core, backtest engine, live engine
  report/                metrics, trade log (CSV/SQLite), equity chart
  cli.py                 `autotr` command
tests/                   unit tests + backtest/replay/restart equivalence + fake-Binance feed test
```
