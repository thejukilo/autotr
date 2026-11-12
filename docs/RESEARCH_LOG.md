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

### Round 3 results
| Config | R1 | R2 | V1 | V2 | Verdict |
|---|---|---|---|---|---|
| xs_mom_trail_a | +6.4% PF1.21 | +10.0% PF1.28 | +3.7% PF1.23 | +6.0% PF1.19 | passes criteria; shorts carry it (long ≈ +0.01R) |
| xs_mom_trail_b | +9.7% PF1.34 | +12.4% PF1.50 | +6.7% PF1.56 | +2.1% PF1.09 | 4/4 positive; both sides positive |
| donchian_trail | +0.9% | −2.9% | −10.3% | +0.6% | dead: fees fixed but no gross edge |
| donchian_trail_b | −0.1% | −2.7% | −9.2% | −5.8% | dead |

### Robustness grid around xs_mom_trail_a (16 variants)
All 16 have positive trade-weighted avg R; 8 are positive in all 4 windows. k is the key parameter (k2 +0.160R, k3 +0.085, k4 +0.017, k5 +0.003).
Lookback 7–14d works, 4d doesn't; stop 3–4 ATR fine, 2 ATR breaks V2; trail 3 ATR steadiest; 1h bars swing +46%/−25%;
higher fees cost ~2 points/window; EMA filter not needed (noema ≥ base). short_only +10–16% in every window, long_only lost
17.8% in V1 → edge is mostly shorting the weakest coins in bear markets.

### Round 4 (R/V)
| Config | R1 | R2 | V1 | V2 | Pos |
|---|---|---|---|---|---|
| xs_mom_k2_noema | +17.0% PF1.68 | +15.2% PF1.46 | +10.1% PF1.65 | +4.3% PF1.15 | 4/4 |
| xs_mom_k1_noema | +7.2% | +16.5% | +1.9% | −3.1% | 3/4 |
| xs_mom_b_ts20 | +7.0% | +18.4% | +4.3% | −2.9% | 3/4 |
| xs_mom_b_lb126 | +17.9% | +3.8% | +0.6% | +0.4% | 4/4 (thin in V) |
| xs_mom_b_k3 | +14.4% | −2.8% | +9.1% | −1.6% | 2/4 |
| xs_mom_b_ts20_lb126 | +12.3% | +4.6% | +0.6% | +3.9% | 4/4 (thin in V) |

## Holdout pre-registration (decided BEFORE running any holdout window)
Finalists, chosen on consistency across R1/R2/V1/V2 (not peak return):
1. **xs_mom_k2_noema**: primary candidate (k=2, 7d lookback, no trend filter, 3 ATR stop + trail; simplest, avg R +0.16)
2. **xs_mom_trail_b**: k=2, 14d lookback, 3.5 ATR stop/trail, both sides positive in research
3. **xs_mom_trail_a**: the original candidate (k=3, EMA50), as reference
Each is run once on H1 (2026-07-15..09-15) and H2 (09-16..11-10). No changes afterwards based on holdout results.
Success = positive in both holdout windows.

## Holdout results (each finalist run once, no changes)
| Config | H1 (07-15..09-15) | H2 (09-16..11-10) | Passes |
|---|---|---|---|
| **xs_mom_k2_noema** | +6.1% PF1.25, 60 tr, +0.112R | +8.3% PF1.45, 53 tr, +0.160R | **yes** |
| **xs_mom_trail_a** | +19.0% PF1.99, 58 tr, +0.328R | +5.1% PF1.28, 53 tr, +0.113R | **yes** |
| xs_mom_trail_b | +3.3% PF1.16, 51 tr, +0.082R | −6.2% PF0.73, 56 tr, −0.108R | no |

Holdout regime: H1 BTC +16.3%, median coin +9.6%, 23/34 coins up (**bull market**, the first non-bear window);
H2 BTC −2.4%, median −3.3%, 15/34 up (flat/down, wide dispersion).

## Conclusion
**Best strategy: xs_mom_k2_noema** (pre-registered primary). Every 4h, rank all coins by 7-day return; long the top 2, short
the bottom 2; 3 ATR(4h) initial stop, chandelier trail 3 ATR behind the best close, no target, 10-day time stop, 1% risk.
Positive in all 6 windows (16 months, bear + bull + flat): +17.0, +15.2, +10.1, +4.3, +6.1, +8.3% (PF 1.15–1.68), ≈ +0.15R per trade,
~1 trade/day, survives ~2× fees. xs_mom_trail_a (k=3, EMA50) also passes all 6 windows and is a close second.

Limits: small edge per trade; profit relies on a few +4–8R trend trades; 34 of today's coins (survivorship); simulated fills;
the holdout is now spent, so further tuning would need new data (e.g. paper trading forward from 2026-11-11).
