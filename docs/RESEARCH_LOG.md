# Strategy research log

## Method
- **Creator** (main Claude session) writes strategies in `src/autotr/strategy/research/<name>.py` + `config/research/<name>.yaml`.
- **Testers** (helper agents) run `uv run autotr research-eval --config config/research/<name>.yaml`, analyse the trades, and write `reports/research/<name>/analysis.md`.
- Live progress: `tail -f reports/research/progress.log`

### Data windows (34 coins, 1m bars, fees 0.05% taker / 0.02% maker, 2 bp slippage)
| Window | Use | Period |
|---|---|---|
| R1, R2 | research: full diagnostics, ideas are developed on these | 2025-08-01..10-31, 2026-01-01..03-31 |
| V1, V2 | validation: accept/reject only, no tuning on trade details | 2025-11-01..12-31, 2026-04-01..06-30 |
| H1, H2 | holdout: locked, only for final candidates | 2026-07-15..09-15, 2026-09-16..11-10 |

Caveats: H1/H2 overlap the period used to develop the confluence strategy (not the new ones). The coin list is today's,
so coins that died since 2025 are missing (survivorship bias). Some coins (e.g. HYPE, TRUMP, 1000BONK) have no data in 2025
and simply aren't traded there.

### Pass criteria for a "winner"
- Profit factor ≥ 1.15 and positive return in every research **and** validation window, ≥ 150 trades in total.
- Then positive in both holdout windows.

## Round 0: what we already know
- 1m RSI-bounce scalper: −92% (PF 0.65). Fees dominate.
- Confluence (24h movers, S/R + channels + trendlines + Elliott, ladder): −84% (PF 0.75).
- Confluence optimized (15m, score ≥4, 60/40 ladder, wait for hold): +3% train, **−26% out-of-sample**. Did not generalise.
- Lessons: high trade frequency on low timeframes is killed by fees; fading big movers into the move loses.

## Round 1 (batch 1)
| Strategy | Idea |
|---|---|
| donchian_trend | 1h 48-bar breakout with EMA100 filter, 2 ATR stop, 2.5R target |
| bb_reversion | 1h 2.5σ Bollinger fade + RSI extreme, only when ADX < 22, target middle band |
| squeeze_breakout | 1h Bollinger-width squeeze then band break, 2.5R |
| trend_pullback | 4h EMA trend, 15m RSI dip/turn entry, 2R |
| exhaustion_fade | 24h mover >8% + 1h RSI ≥78 + reversal bar → fade to EMA20 |

### Round 1 results
| Strategy | R1 | R2 | V1 | V2 | Trades | Verdict |
|---|---|---|---|---|---|---|
| donchian_trend | −9.8% PF0.95 | +17.9% PF1.06 | −30.7% PF0.80 | −27.4% PF0.88 | ~1390 | dead; gross edge positive in R but fees eat it |
| bb_reversion | −9.0% PF0.65 | −10.2% PF0.74 | +17.3% PF1.74 | +22.1% PF1.72 | 202 | regime-dependent; no gross edge in R |
| squeeze_breakout | −18.6% | −35.6% | −38.9% | −31.1% | ~1700 | dead |
| trend_pullback | −89.3% | −82.8% | −58.8% | −72.5% | ~4400 | dead (15m entries → 12 trades/day, fees) |
| exhaustion_fade | −3.0% | −7.2% | +1.3% | +7.1% | 154 | marginal/dead |

**Learnings (testers A+B):**
- Across all strategies, trades closed within a few hours lose; trades held 12–48h win → stops too tight / false breakouts. Slower timeframes + wider stops.
- Low-timeframe entries (15m) trade 12×/day and fees alone sink them.
- All 4 windows are bear markets (median coin −18% to −37%). R windows slightly trendier (efficiency ratio 0.16–0.17) than V (0.14–0.15) → breakouts did better in R, fades in V.

## Round 2
| Strategy | R1 | R2 | V1 | V2 | Verdict |
|---|---|---|---|---|---|
| bb_reversion_v2 (re-entry, wider stop, ER filter) | −16.0% | −49.7% | +3.4% | +15.1% | worse than v1 (5× trades) — dead |
| xs_momentum (4h, rank 7d return, top/bottom 3, 3R) | +4.2% PF1.08 | +6.5% PF1.11 | −3.8% PF0.89 | +0.7% PF1.02 | best so far: gross edge in both R; no side bias; profit from a few big trades → cap at 3R wastes the tail |

## Round 3 (trailing stops; engine now supports `manage(st, pos)` to tighten the stop)
| Config | Idea |
|---|---|
| xs_mom_trail_a | xs momentum k=3, 7d, 3 ATR stop + 3 ATR chandelier trail, no target |
| xs_mom_trail_b | k=2, 14d momentum, 3.5 ATR stop/trail |
| donchian_trail | 4h Donchian-30, EMA50, 2.5 ATR stop, 3 ATR trail |
| donchian_trail_b | 4h Donchian-60, 3 ATR stop, 3.5 ATR trail |
