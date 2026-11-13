# Scalping research log (top movers, Binance USDT-M)

## Research

Researcher pass, 2026-11-13. Input: `docs/SCALP_BRIEF.md`, `docs/RESEARCH_LOG.md`, README "Current results".
The WebSearch tool failed with an API error, so I found sources through the arXiv API and checked
bibliographic details (title, venue, DOI) through Crossref. Where a finding is from my own memory of a paper and
I could not reread it, it says "(from memory)".

### Facts that constrain every design
- **Cost in R units.** Risk is 1% of equity per trade, so the cost in R is `round_trip% / stop_distance%`.
  | Entry / exit | Round trip | Cost at a 1% stop | at 2.5% | at 4% |
  |---|---|---|---|---|
  | market in + stop out | 0.20% | 0.20R | 0.08R | 0.05R |
  | market in + maker TP | 0.12% | 0.12R | 0.05R | 0.03R |
  | limit in + stop out | 0.12% | 0.12R | 0.05R | 0.03R |
  | limit in + maker TP | 0.04% | 0.04R | 0.016R | 0.01R |
  Rule used below: designs with a market leg need a stop distance of **at least 2-2.5%**. Maker/maker designs can go down to about 1.2%.
  Movers usually have a 5m ATR of 0.5-1.5%, so a stop like this is still only 2-4 ATR(5m).
- **Trade count.** Every earlier 1m/5m system made 5-13 trades a day and lost 60-90%. Target ≤ 2-3 trades a day across the whole book,
  using event triggers and a per-symbol daily cap (`max N entries per symbol per UTC day`, kept as state inside the strategy).
- **Adverse selection on limit entries.** In the sim a rung fills only when price trades *through* it. Pullback rungs therefore fill
  most often on the trades where the pullback keeps going, and the best trades (no pullback) never fill. Every ladder design
  must be compared with a market-entry twin, or the fill rate and win rate can't be interpreted.
- **1m OHLC pessimism.** If the stop and the TP are both inside one candle, the stop is assumed first. The engine also never lets a TP
  fill on the same bar as a limit entry. Designs whose TP is within about 1 ATR(1m) of entry are biased against, so keep targets wide.
- **Data available to strategies.** `Bar` has OHLC + volume + quote_volume (no taker-buy volume). `SymbolState` has **no funding
  rate** (funding is only charged). Structure deques (`t_s/h_s/l_s/c_s`) have no volume, so VWAP must come from `bars_e`. A rolling
  24h VWAP therefore needs `entry_tf 5 + entry_history_bars 288` (or 15m × 96). That fits in the ~2 days of pre-mover history.
- **Lessons from the momentum search.** Fading big movers into the move lost money. Short holds lost and long holds won (stops too tight, false
  breakouts). Uncapped trailing exits caught the profit tail and fixed targets cut it off. The edge that survived holdout was continuation
  (momentum), mostly on the short side in bear markets.

### What the literature and practice suggest
1. **Intraday momentum exists after large moves, and so does reversal.** In equities, the first half-hour return predicts the last half-hour
   (Gao, Han, Li, Zhou 2018, JFE). The same intraday time-series momentum has been documented for Bitcoin (Shen, Urquhart, Wang 2022,
   Financial Review). For a cross-section of cryptocurrencies, Wen, Bouri, Xu, Zhao (2022, NAJEF) find intraday momentum *and* reversal,
   with which one dominates depending on conditions such as jumps and liquidity (from memory). Caporale & Plastun (2020, FMPM): on days with
   abnormal returns, crypto prices tend to keep moving in the direction of the abnormal move within the day (momentum), and effects after that day are weak
   (from memory). → Trade *with* the day's move intraday, with short horizons and trailing exits.
2. **Pumps revert.** Coordinated pump-and-dumps peak within minutes and then give back most of the move (Xu & Livshits 2019; La Morgia et al. 2020/2023).
   Lottery-like coins with extreme daily maxima underperform afterwards (Li et al. 2021, MAX effect). Cryptocurrency momentum
   reverses at multi-week horizons (Dobrynskaya 2021). → Fade only *after* the move has visibly failed (e.g. lost VWAP), not into strength.
   Our movers have ≥ $50M volume, so they are less purely manipulated than Telegram pumps, but the late-pump behaviour is similar.
3. **Liquidation cascades overshoot.** Forced liquidations on perpetuals are large relative to open interest, and liquidated traders use very
   high leverage (arXiv 2102.04591: daily forced liquidations of about 3.5% (longs) and 1.9% (shorts) of outstanding futures on BitMEX, average leverage of liquidated traders about 60x). Liquidations are market orders that eat the book, which produces wicks that bounce back within minutes. Catching these
   wicks with resting bids is a common practitioner technique. With only 1m OHLC we can detect the cascade only as a
   large-range, high-volume 1m/5m candle at a new local extreme.
4. **Opening-range breakouts on "stocks in play".** Zarattini, Barbon, Aziz (2024) report that a 5-minute ORB traded only on stocks with
   high relative volume, with a stop at 10% of the 14-day ATR and an exit at end of day, beat buy-and-hold net of costs. The selection effect
   ("in play") does most of the work. Our top movers are the crypto equivalent of stocks in play. Crypto has no opening bell, but
   activity clusters at specific UTC hours. Bitcoin volume and volatility peak around the European/US overlap, roughly 13-16 UTC (Eross et al. 2019; Baur et al.
   2019). 00:00 UTC is the daily-candle reset that many traders use as an anchor.
5. **VWAP as the trend filter and value anchor.** Zarattini & Aziz (2023) show a simple long-above/short-below-VWAP intraday trend system
   working on QQQ. Practitioner day-trading of momentum names uses "hold above VWAP = buy pullbacks, first VWAP loss = short the retest".
6. **Funding.** The perpetual price minus spot is pulled back by funding (He, Manela, Ross, von Wachter, "Fundamentals of Perpetual Futures").
   On a top mover, extreme negative funding while price rises means crowded shorts, which is fuel for a squeeze. Extreme positive funding means
   crowded longs. Using funding needs an engine change (expose the last/next funding rate in `SymbolState`), so it is only proposed as a filter.
7. **Maker-only designs.** These remove about 80% of fees, but adverse selection (fact 3 above) becomes the main risk. Use them where the
   *event* (a flush, a test of an overextended band) is what is being bought, not a routine pullback.

### Candidates (ranked by prior)
Common block as in the brief, unless a value is listed. Definitions: `ATRk(n)` = ATR on k-minute bars over n bars; `VWAP24` = rolling
VWAP of typical price (h+l+c)/3 weighted by volume over the last 24h of entry-TF bars; `dir` = sign(change_24h);
"up-mover" means change_24h ≥ threshold. All candidates keep a per-symbol daily entry cap inside the strategy.

**1. `flush_catch`: liquidation-wick catch, maker in / maker out (trend-aligned)**
- TF: entry 1m, structure 5m × 288, entry_history_bars 240, time_stop 90, cooldown 60, ladder_expiry_bars 10.
- Filter: |change_24h| ≥ 0.10. Primary: trend-aligned only (buy down-flushes in up-movers, sell up-spikes in down-movers).
  Variant B: both directions.
- Trigger (long): over the last ≤ 5 closed 1m bars, window high → current close drop `D = win_high − win_low` with
  `D/price ≥ max(3.0%, 6 × ATR1(60)/price)`. Sum of window volume ≥ 4 × (5 × median 1m volume of the last 120 bars). Window low = lowest low
  of the last 240 bars (a new 4h low, a stop run). Close is in the lower 40% of the window range.
- Entry: buy limits at `close − 0.15·D` (w 0.5) and `close − 0.35·D` (w 0.5).
- Stop: `min(win_low − 0.40·D, rung2 × 0.985)`. Skip if the stop is > 6% from the average rung.
- TP (maker): `win_low + 0.50·D` (50% retracement of the flush). Variant: 0.618.
- manage(): after 30 min, if the high since entry ≥ avg_entry + 0.25·D, move the stop to break-even.
- Cap 2 entries per symbol per day. Expect about 0.5-1.5 trades a day.

**2. `coil_break`: post-move consolidation (flag) breakout with chandelier trail**
- TF: entry 15m, structure 60m × 24, entry_history_bars 96, time_stop 360, cooldown 120.
- Filter: |change_24h| ≥ 0.20, side = dir.
- Coil (long): the last 12 bars (3h) have range `R = max(h) − min(l)` ≤ min(3.5 × ATR15(14), 0.35 × |close − close[96 bars ago]|).
  The coil low is above the 24h-range 60% level (it holds near the highs).
- Trigger: 15m close > coil high + 0.1·ATR15, bar volume ≥ 1.5 × the 20-bar mean.
- Entry: market. Stop: coil low − 0.2·ATR15, or coil midpoint if R/price > 6%. Require a stop distance of 2-7%.
- Exit: no TP. manage() chandelier 3.0 × ATR15 from the highest close, tightened to 2.0 × ATR15 after +1.5R. Time stop 360m.
- Cap 1 entry per symbol per day. Expect about 0.5-1 trade a day.

**3. `vwap_pullback`: trend-of-day pullback to the EMA/VWAP zone, ladder entry**
- TF: entry 5m, structure 15m × 96, entry_history_bars 288, time_stop 240, cooldown 60, ladder_expiry_bars 6.
- Filter: |change_24h| ≥ 0.15, side = dir. Trend (long): close > VWAP24, EMA20(5m) > EMA50(5m), last 15m close > EMA20(15m).
  A new 24h high was printed within the last 24 bars (2h).
- Setup: close ≤ (2h high − 1.5 × ATR5(14)) and close > VWAP24.
- Entry: buy limits at `close − 0.5·ATR5` (w 0.5) and `max(close − 1.25·ATR5, VWAP24 × 1.002)` (w 0.5).
- Stop: `min(rung2 − 1.5·ATR5, VWAP24 − 0.5·ATR5)`, with a stop distance of 1.5-6% from the average rung.
- TP (maker): 2h high + 0.25·ATR5, only if reward/risk ≥ 1.0. manage(): break-even after +1R.
- Twin to run alongside: identical rules with a market entry at the signal (this measures adverse selection).
- Cap 2 entries per symbol per day.

**4. `vwap_fail_short`: first VWAP loss after a blow-off, short the retest**
- TF: entry 15m, structure 60m × 24, entry_history_bars 96, time_stop 360, cooldown 240, ladder_expiry_bars 4.
- Filter: change_24h ≥ +0.25. The 24h max high ≥ VWAP24 × 1.12 (stretched). The 24h high was set 2-16h ago (8-64 bars).
- Trigger: the *first* 15m close below VWAP24 since that high, with bar volume ≥ 1.0 × the 20-bar mean.
- Entry: sell limits at VWAP24 (w 0.5) and VWAP24 + 0.5·ATR15 (w 0.5). If neither fills, no trade (variant B: market).
- Stop: `max(high of last 8 bars, rung2 + 1.5·ATR15)`, with a stop distance of 2-8%.
- TP (maker): avg_entry − 2.0R. manage(): break-even once price has moved +1R in the trade's favour.
- Mirror for down-movers (first VWAP reclaim → long the retest) as a separate variant with a weaker prior (squeezes).
- Cap 1 per symbol per day.

**5. `session_orb`: opening-range breakout at 00:00 and 13:30 UTC on movers (Zarattini-style)**
- TF: entry 5m, structure 15m × 96, entry_history_bars 288, time_stop 360, cooldown 0 (one trade per symbol per session).
- OR = the first 6 × 5m bars after each session open. Valid if OR height is 1.0-5.0% and OR volume ≥ 2 × the mean 30-min volume of the prior 24h
  (relative-volume "in play" filter).
- Filter: |change_24h| ≥ 0.10, side = dir, close on the dir side of VWAP24.
- Trigger: a 5m close beyond the OR high (long) within 120 minutes of the OR end. Entry: market.
- Stop: opposite side of the OR (OR midpoint if height > 4%), with a stop distance of at least 1.5%.
- Exit: no TP. manage() chandelier 3 × ATR5 after +1R. Time stop 360m.
- Variants: 00:00 only / 13:30 only (time-of-day test).

**6. `ignition`: newly igniting mover, buy the first shallow dip**
- TF: entry 5m, structure 15m × 96, entry_history_bars 288, time_stop 180, cooldown 180, ladder_expiry_bars 3.
- Trigger (long): 1h return (12 bars) ≥ +6%, last-hour volume ≥ 4 × the mean hourly volume of the prior 23h, close at a new 24h high,
  change_24h ≥ +0.12. Mirror for dumps.
- Entry: buy limits at `close − 0.5·ATR5` (w 0.5) and `close − 1.0·ATR5` (w 0.5). Variant B: market.
- Stop: `rung2 − 2.0·ATR5`, with a stop distance of at least 2.5%.
- Exit: no TP. manage() chandelier 3 × ATR5 from the highest close once +1R is reached. Time stop 180m.
- Cap 1 per symbol per day.

**7. `xs_intraday`: cross-sectional momentum inside the mover set (the edge that survived holdout, shortened)**
- TF: entry 15m, structure 60m × 24, entry_history_bars 96, time_stop 240, cooldown 240. Uses `observe()`.
- Rebalance at 00/04/08/12/16/20 UTC. Over the scanner's current movers, score = 4h return / (ATR15(14)/price).
  Long the top-1 if its score > 0 and it is above VWAP24. Short the bottom-1 if its score < 0 and it is below VWAP24. At most 2 new trades per rebalance.
- Entry: market. Stop: 3 × ATR15. Exit: chandelier 3 × ATR15, no TP, time stop 240m.
- Variant: short-only (that was the side that carried the 4h momentum strategy).

**8. `sigma_fade`: maker-only fade of extreme stretch in a non-trending mover**
- TF: entry 5m, entry_history_bars 288, time_stop 120, cooldown 120, ladder_expiry_bars 6.
- Filter: |change_24h| ≥ 0.10. Efficiency ratio ER(48, 5m) ≤ 0.30 (choppy, not a clean trend). For shorts: RSI14 on 15m ≥ 75
  (mirror ≤ 25 for longs).
- Arm: a 5m close ≥ SMA48 + 2.5·σ48. Entry: sell limits at SMA48 + 3.0σ (w 0.5) and + 3.75σ (w 0.5).
- Stop: SMA48 + 5.0σ, with a stop distance of at least 1.5%. TP (maker): SMA48 + 1.0σ. Time stop 120m.
- Low prior: bb_reversion was regime-dependent, and fading movers lost before. Kept as the mean-reversion control.

**9. `funding_filter`: funding-crowding filter on top of #3 and #4 (needs an engine change)**
- Expose the latest funding rate (normalised to 8h) in `SymbolState` (a `market/` change, which must be logged).
- #3 longs allowed only if funding ≤ +0.05%/8h. Preferred when funding ≤ −0.10%/8h (crowded shorts on a rising coin = squeeze fuel).
- #4 shorts allowed only if funding ≥ +0.03%/8h (crowded longs). Skip if funding ≤ −0.10% (squeeze risk).
- Test only if #3 or #4 shows a gross edge on SR. The filter halves the trade count, so it needs both SR windows to be meaningful.

### Suggested process
1. Run #1, #2, #3 (+ market twin) and #4 first. Report for each: trades/day, fill rate of ladders, gross vs fees vs funding,
   avg R, PF, and the split by side and by hold-time bucket.
2. Diagnostics on SR trades: does gross avg R increase with |change_24h| (20-40% vs > 40%)? With UTC hour? With entry within the first
   6h of becoming a mover vs later? Those three cuts decide filters. Do not add any filter that was not motivated by an SR cut.
3. Kill rule: gross avg R ≤ +0.05 on SR1+SR2 combined means no edge to protect from fees, so drop the candidate rather than tune it.

### Sources
- Gao, Han, Li, Zhou (2018), Market intraday momentum, JFE. https://doi.org/10.1016/j.jfineco.2018.05.009
- Shen, Urquhart, Wang (2022), Bitcoin intraday time series momentum, Financial Review. https://doi.org/10.1111/fire.12290
- Wen, Bouri, Xu, Zhao (2022), Intraday return predictability in the cryptocurrency markets: Momentum, reversal, or both, NAJEF. https://doi.org/10.1016/j.najef.2022.101733
- Caporale, Plastun (2020), Momentum effects in the cryptocurrency market after one-day abnormal returns, FMPM. https://doi.org/10.1007/s11408-020-00357-1
- Eross, McGroarty, Urquhart, Wolfe (2019), The intraday dynamics of bitcoin, RIBAF. https://doi.org/10.1016/j.ribaf.2019.01.008
- Baur, Cahill, Godfrey, Liu (2019), Bitcoin time-of-day, day-of-week and month-of-year effects in returns and trading volume, FRL. https://doi.org/10.1016/j.frl.2019.04.023
- Zarattini, Barbon, Aziz (2024), A Profitable Day Trading Strategy for the U.S. Equity Market (ORB on stocks in play). https://doi.org/10.2139/ssrn.4729284
- Zarattini, Aziz (2023), Volume Weighted Average Price (VWAP): The Holy Grail for Day Trading Systems? https://doi.org/10.2139/ssrn.4631351
- Xu, Livshits (2019), The Anatomy of a Cryptocurrency Pump-and-Dump Scheme. https://arxiv.org/abs/1811.10109
- La Morgia et al. (2020), Pump and Dumps in the Bitcoin Era: Real Time Detection of Cryptocurrency Market Manipulations. https://arxiv.org/abs/2005.06610
- La Morgia et al. (2023), The Doge of Wall Street: Analysis and Detection of Pump and Dump Cryptocurrency Manipulations. https://arxiv.org/abs/2105.00733
- Li et al. (2021), Lottery-like preferences and the MAX effect in the cryptocurrency market, Financial Innovation. https://doi.org/10.1186/s40854-021-00291-9
- Dobrynskaya (2021), Cryptocurrency Momentum and Reversal. https://doi.org/10.2139/ssrn.3913263
- Liquidation, Leverage and Optimal Margin in Bitcoin Futures Markets (2021). https://arxiv.org/abs/2102.04591
- He, Manela, Ross, von Wachter, Fundamentals of Perpetual Futures. https://arxiv.org/abs/2212.06888

## Round 1

Creator pass, 2026-11-13. Six researcher candidates (#1-#6 of the ranking) plus two baselines. All configs use the
common block from `docs/SCALP_BRIEF.md` unchanged. Strategies are in `src/autotr/strategy/research/scalp_<name>.py`, and the shared
helpers (rolling VWAP from `bars_e`, ATR on entry bars, per-symbol daily cap, initial-stop tracking for R-based
break-even/trails) are in `research/_scalp.py`. No engine code was changed.

Not implemented this round: `xs_intraday`, `sigma_fade` (the control) and `funding_filter`, which needs a `market/` change and
should wait until a base strategy shows a gross edge. The market-entry twins are built in but not run, because of the 8-config cap. Set
`params.market: true` on `vwap_pullback`, `vwap_fail_short` or `ignition` to run one.

| Config | Idea | Hypothesis being tested |
|---|---|---|
| `flush_catch` | 1m: a ≥3% / ≥6×ATR1m(60) flush within 5 bars on ≥4× volume, making a new 4h extreme, trend-aligned (buy down-flushes on up-movers, sell spikes on down-movers). Limit rungs at close−0.15D/−0.35D, maker TP at the 50% retrace, break-even after 30m. Max 2 per symbol per day. | Liquidation wicks partly retrace within minutes. With maker in/out (≈0.04%) the gross retrace edge survives costs. |
| `coil_break` | 15m: on ≥20% movers, a 12-bar coil (range ≤ min(3.5 ATR, 0.35 × 24h move)) in the top 40% of the 24h range, broken on 1.5× volume. Market entry, structural stop 2-7%, chandelier 3 ATR (2 ATR after +1.5R), no target. | Continuation on abnormal-return days. A wide structural stop keeps fees small in R, and an uncapped trail keeps the profit tail (the only edge the momentum search found). |
| `vwap_pullback` | 5m: trend of day (close > VWAP24, EMA20 > EMA50, 15m close > EMA20, 24h high in the last 2h). Pullback ≥1.5 ATR from the 2h high. Ladder at −0.5/−1.25 ATR (floored at VWAP), stop below rung2/VWAP (1.5-6%), maker TP at the 2h high + 0.25 ATR (RR ≥ 1), break-even at +1R. | Pullbacks in the day's trend retest the high often enough that a maker/maker trade has a positive gross R. Also measures ladder adverse selection (fill rate). |
| `vwap_fail_short` | 15m: ≥25% up-mover, high ≥ 1.12× VWAP24 set 2-16h ago, first close below VWAP24 since the high. Sell limits at VWAP and VWAP + 0.5 ATR, stop above the 2h high, TP 2R, break-even at +1R. Max 1 per symbol per day. | A pump that has visibly failed (VWAP loss hours after the peak) reverts. This fade avoids the "fade into strength" losses of earlier searches. |
| `session_orb` | 5m: 30-min opening range at 00:00 and 13:30 UTC (height 1-5%, volume ≥ 2× normal). Breakout close in the mover's direction (on the right side of VWAP24) within 2h. Market entry, stop at the opposite OR side (mid if > 4%, at least 1.5%), chandelier 3 ATR after +1R, no target. One trade per symbol per session. | ORB on "stocks in play" (Zarattini et al. 2024) carries over to crypto movers at the daily-candle open and the US open. |
| `ignition` | 5m: 1h return ≥ ±6% on ≥4× hourly volume, closing at a new 24h extreme, \|change_24h\| ≥ 12%. Limits at −0.5/−1.0 ATR, stop rung2 − 2 ATR (≥2.5%), chandelier 3 ATR after +1R, no target. Max 1 per symbol per day. | An attention shock at the start of a move continues intraday. Entering in the first hour, before the coin is a stale top mover, with a trail catches the tail. |
| `base_scalper` | Baseline: the original 1m RSI-bounce scalper with `config/scalper.yaml` settings (S/R from 5m × 288). | Reference: fees should still sink a high-frequency 1m scalper on movers. |
| `base_confluence` | Baseline: confluence with the optimized-2026-11 settings (score ≥ 4, confirm hold, 60/40 ladder, trend filter, 2R, 90m time stop). Structure 15m × 96 (1 day). | Reference: S/R-confluence fades on movers with ≤ 1 day of structure. |

Implementation notes and deviations from the researcher spec:
- The **daily cap** is counted when a signal is emitted (a signal the risk manager rejects still counts). After a signal the symbol is
  silent for `ladder_expiry_bars` entry candles (the ladder may be pending) and while a position is open. Without this, rejected
  "already_in_symbol" signals would use up the cap.
- **R for break-even/trails** = distance from the average entry to the *initial* stop (the first stop value `manage` sees).
- "No TP" strategies use a target 50R away, which never fills.
- `vwap_fail_short`: "first close below VWAP24 since the high" is approximated as every close between the high and the signal bar being
  ≥ the *current* VWAP24. The spec version (vol ≥ 1.0× mean, stop 2-8%) produced only 2 signals on SR1 (signal counts only;
  PnL was not looked at). So the volume filter was dropped (`vol_mult: 0`) and the stop band widened to 1.5-12%, which gives about 19 signals on SR1.
  It is still a low-sample candidate.
- `session_orb`: stops closer than 1.5% are widened to 1.5% (not skipped). The "prior 24h" volume baseline is the bars before the
  session start within the 288-bar history (~21-22h).
- `flush_catch`: the window is the fixed last 5 bars, and the high must come before the low (down-flush) for longs (mirrored for shorts).
- Smoke test (2025-09-01..02): all 8 run. `uv run pytest -q` passes.

Testers: report fill rate (`fills`, cancelled plans) for ladder configs, the diagnostics from "Suggested process", and apply the kill rule
(gross avg R ≤ +0.05 on SR1+SR2).

## Round 2

Creator pass, 2026-11-13.

### Round 1 results (copied from the tester reports)
Return, PF and avg R per window. Research total = SR1+SR2.

| Config | SR1 | SR2 | SV1 | SV2 | Gross vs fees (SR1 / SR2, $) | Verdict |
|---|---|---|---|---|---|---|
| `flush_catch` | -3.6%, 63 tr, PF 0.86, +0.046R | -4.2%, 61, PF 0.82, -0.017R | -13.6%, PF 0.66 | +0.8%, PF 1.02 | -2.51 / -3.09 vs 1.08 / 1.22 | dead |
| `vwap_pullback` | +4.2%, 72, PF 1.22, +0.123R | -7.5%, 89, PF 0.72, -0.037R | +7.4%, PF 1.27 | +1.0%, PF 1.03 | +4.00 / -9.52 vs 0.72 / 0.82 (funding +0.91 / +2.81) | marginal |
| `session_orb` | -0.2%, 15, PF 0.96, -0.012R | -2.9%, 25, PF 0.79, -0.101R | +10.1%, 11 tr, PF 2.97 | -7.1%, 27, PF 0.52 | +0.02 / -3.34 vs 0.27 / 0.66 | dead |
| `base_scalper` | -53.3%, 435, PF 0.73, -0.171R | -48.9%, 438, PF 0.75, -0.149R | -36.1% | -56.5%, PF 0.68 | -26.6 / -22.1 vs 24.5 / 26.8 | dead (negative control) |
| `coil_break` | +35.9%, 50, PF 2.49, +0.67R | -14.1%, 66, PF 0.58, -0.23R | +12.5%, 62, PF 1.44, +0.20R | -14.5%, 59, PF 0.54, -0.26R | +36.5 / -13.5 vs 1.13 / 1.12 | marginal (SR1 = MYXUSDT) |
| `vwap_fail_short` | -1.0%, 12, PF 0.74, -0.08R | -2.9%, 10, PF 0.34, -0.28R | -2.2%, 6, PF 0.38 | -1.0%, 6, PF 0.57 | -0.84 / -2.38 vs 0.09 / 0.07 | dead |
| `ignition` | -0.6%, 69, PF 0.98, +0.02R | +0.2%, 42, PF 1.01, -0.00R | +10.5%, 39, PF 1.75, +0.46R | -4.0%, 45, PF 0.77, -0.07R | +0.01 / -0.66 vs 0.68 / 0.46 | marginal |
| `base_confluence` | +0.0%, 7, PF 1.00, +0.04R | +1.2%, 8, PF 1.38, +0.26R | -3.0%, 3, PF 0.00 | +1.5%, 6, PF 1.67 | +0.19 / +1.66 vs 0.19 / 0.17 | dead (24 trades in 12 weeks) |

Dropped: `flush_catch` (no gross edge; adverse selection on the deep rung), `session_orb` (40 research trades, no gross
edge; the 00:00-only split is n=9), `vwap_fail_short` (all 4 windows lose, 22 research trades), and both baselines (kept
as references, not tuned). Fees are no longer the problem for any surviving design. The losses are in the signal.

Common pattern in the research windows (SR only) of all three surviving ideas: trades closed in under 30 min-2h are
stop-outs, and the money is in 2-8h holds that were still open when the time stop closed them. Shorts are flat or
negative (and rare). Entries at 18-24 UTC lose in both SR windows for `coil_break` and `ignition`. My own SR check of the
tester CSVs (`trades_SR*.csv`, research windows only), long trades with signal hour < 18 UTC:
`coil_break` SR1 n28 +1.26R / SR2 n33 -0.16R (all trades +0.67 / -0.23R); `ignition` SR1 n52 +0.07R / SR2 n32 +0.24R
(all trades +0.02 / -0.00R). For `vwap_pullback` the same cut is inconsistent (SR1 -0.09R, SR2 -0.03R), so it does not get it.

### Round 2 configs

| Config | Idea | Hypothesis being tested |
|---|---|---|
| `coil_break_r2` | `coil_break` with three changes: longs only, no signals 18-24 UTC, time stop 360 → 720 min. Everything else unchanged. | The SR1 edge isn't only MYX: once shorts (≤0R in both SR windows) and the 18-24 UTC entries (negative in both) are removed and winners aren't cut at 6h, SR2 should move to ≥0R. If SR2 is still negative, coil_break is dead. |
| `ignition_r2` | `ignition` (limit ladder at -0.5/-1.0 ATR5) with longs only, no signals 18-24 UTC, time stop 180 → 600 min. | The 180-min time-stop exits (+0.9R / +1.8R, 67-80% winners) are the edge; a longer hold lets the trail catch the tail and pushes gross avg R above the +0.05 kill line in both SR windows. |
| `ignition_mkt_r2` | `ignition_r2` with a market entry at the signal close (stop still rung2 - 2 ATR5, at least 2.5% from entry). | The ladder twin required by the brief. ~30% of ladders expired unfilled, probably the strongest ignitions. If the market version has a higher gross R per trade after its higher fees (~0.04R), the dip-buy rungs are adversely selected. |
| `vwap_pullback_r2` | `vwap_pullback` with a single limit rung at close - 0.5 ATR5 (no VWAP rung). The stop is min(rung - 1.5 ATR5, VWAP24 - 0.5 ATR5), still 1.5-6%. TP, break-even and filters unchanged. | The deep VWAP rung is toxic (2-rung trades -0.14R / -0.32R, 17% win in SR2). Caveat: the round-1 "1-rung trades were positive" result is conditioned on the price *not* reaching rung 2, so it overstates what a single rung earns. This config measures the real effect. |
| `vwap_pullback_mkt_r2` | `vwap_pullback_r2` with a market entry at the signal close (same stop/TP levels; the min_stop/RR checks use the close). | The market twin (tester idea 1 and the brief's rule for ladder designs): does the shallow pullback itself carry an edge, or only the fills? |
| `xs_intraday` | New (researcher #7): every 4h (00/04/.../20 UTC) rank the movers (\|change_24h\| ≥ 10%, ≥ $50M volume) by 4h return / ATR15%. Long the top 1 if it is above VWAP24, short the bottom 1 if it is below. Market entry, stop 3 ATR15 (≥ 2%), chandelier 3 ATR15, no TP, time stop 240, max 2 per symbol per day. | The cross-sectional momentum that survived holdout on 4h bars also works intraday inside the mover set. Continuation plus a trail is the one pattern that has worked so far. |

Implementation notes:
- New params with defaults that keep round-1 behaviour (round-1 configs are unchanged and still run): `longs`, `shorts`,
  `skip_hours` (UTC hour of the signal candle's close time, which is also the market-entry time) in `coil_break`, `ignition`
  and `vwap_pullback`, plus `single` in `vwap_pullback`. Helpers `side_ok` / `hour_ok` are in `_scalp.py`.
- `xs_intraday`: the strategy can't see the scanner, so it ranks every symbol that passes the change/volume filters in
  `observe()`. Symbols are processed one at a time on a candle, so on a rebalance candle some scores are one 15m candle
  old (the same approximation as `xs_momentum`). Scores more than one candle old are ignored. The smoke test gave ~6
  trades a day, which is more than the ≤ 2-3/day target. Stops are 2-5% with a market entry, so fees are ~0.05R per trade.
  Testers: report fees vs gross.
- Two round-2 changes per idea (side/hour filter + time stop) are bundled because both come from the same consistent SR
  splits and the 6-config cap doesn't allow separate ablations. If a config improves, a later round can ablate them.
- Smoke test (2025-09-01..02): all 6 run. `uv run pytest -q` passes. No engine code was changed.

Testers: apply the same kill rule (gross avg R ≤ +0.05 on SR1+SR2). For each ladder/market pair, compare the gross R per signal
and the fill rate. For `coil_break_r2` and `ignition_r2`, report the hold-time and exit-reason split again (time-stop exits
vs trail exits at 720/600 min).

## Round 3

Creator pass, 2026-11-13.

### Round 2 results (copied from the tester reports)
Return, trades, PF and avg R per window. Gross = price PnL before fees and funding.

| Config | SR1 | SR2 | SV1 | SV2 | Gross / fees / funding (SR1; SR2, $) | Verdict |
|---|---|---|---|---|---|---|
| `coil_break_r2` | +36.8%, 29, PF 3.89, +1.18R | -6.7%, 33, PF 0.63, -0.20R | +24.6%, PF 3.16, +0.81R | -2.6%, PF 0.87, -0.07R | +37.0 / 0.63 / +0.4; -7.4 / 0.56 / +1.3 | dead (pre-registered rule: SR2 still negative; SR1 = 3 MYX trades) |
| `ignition_mkt_r2` | +46.1%, 64, PF 2.29, +0.65R | +3.2%, 43, PF 1.16, +0.08R | +12.0%, PF 1.57, +0.24R | +5.5%, PF 1.23, +0.11R | +46.5 / 0.98 / +0.6; -0.20 / 0.60 / +4.0 | promising (SR2 profit = funding; tail-dependent) |
| `vwap_pullback_mkt_r2` | -3.7%, 44, PF 0.80, -0.08R | +0.3%, 61, PF 1.01, +0.01R | -1.5%, PF 0.95 | +6.6%, PF 1.18, +0.07R | -3.43 / 0.76 / +0.5; -2.90 / 1.10 / +4.3 | dead |
| `ignition_r2` | +2.5%, 52, PF 1.10, +0.113R | +6.8%, 32, PF 1.47, +0.203R | +7.2%, 35, PF 1.57, +0.413R | -4.4%, 34, PF 0.70, -0.120R | +2.82 / 0.53 / +0.25; +5.23 / 0.37 / +1.92 | promising (weak; top 3 trades) |
| `vwap_pullback_r2` | +6.7%, 64, PF 1.28, +0.110R | +1.9%, 77, PF 1.06, +0.028R | +8.0%, 83, PF 1.27, +0.099R | +1.1%, 103, PF 1.03, +0.016R | +6.78 / 0.82 / +0.77; -0.37 / 1.00 / +3.24 | marginal (gross +0.045R, at the kill line) |
| `xs_intraday` | +10.1%, 114, PF 1.30, +0.091R | -12.3%, 152, PF 0.72, -0.087R | +16.1%, 125, PF 1.43, +0.127R | +10.8%, 116, PF 1.35, +0.091R | +10.33 / 1.48 / +1.21; -11.86 / 1.91 / +1.45 | marginal (research gross -0.006R) |

### Decisions
- Dropped `coil_break_r2` (its own pre-registered kill condition), `vwap_pullback_mkt_r2` (negative price gross in both SR).
- Dropped `vwap_pullback_r2` and `xs_intraday` by the kill rule (combined SR gross avg R ≤ +0.05: +0.045R and -0.006R).
  Both were rated marginal on the strength of validation, but validation can't be used to keep a candidate the research
  windows reject; the rule says drop rather than tune. My SR re-cuts below found no clean, consistent split to justify a
  repair either (xs longs: +0.32R / -0.13R price-only; vwap entries within 2.5% of VWAP24: n13/n16, too thin).
- Kept only the `ignition` family, which is the only design with positive price gross in both SR windows for at least one
  entry type (ladder: +2.82 / +5.23).

### Round-3 evidence (SR1/SR2 only)
I re-ran `ignition_mkt_r2`, `ignition_r2`, `vwap_pullback_r2` and `xs_intraday` with `autotr backtest` on the SR1 and SR2
dates only (no validation, no holdout) to get the full trades.csv (`qty`, `risk_usd`, `gross_pnl`, `funding`). Trade counts
match the tester CSVs exactly.

**Warning for testers: the `stop` column in `trades_SR*.csv` is the *final* stop (after break-even/trailing), not the
initial stop.** Any cut on "stop distance" from that column is lookahead: winners have trailed stops, so they look like
"wide-stop" (ignition, xs) or "tight-stop" (vwap_pullback break-even) trades. My first pass found apparently perfect
4-of-4 cuts this way; with the true initial distance (`risk_usd / qty / entry_price`) all of them vanish. No
stop-distance filter is used.

Cuts that survived (price-only R = gross_pnl / risk_usd, change_24h at the signal from the entry reason):

| Cut | ignition_mkt_r2 SR1 | SR2 | ignition_r2 SR1 | SR2 |
|---|---|---|---|---|
| change_24h < 30% | n21 +0.70 | n17 +0.63 | n16 +0.04 | n13 +0.88 |
| change_24h ≥ 30% | n43 +0.62 | n26 -0.41 | n36 +0.15 | n19 -0.35 |
| change_24h 25-40% | n21 -0.39 | n16 +0.03 | n18 -0.45 | n12 -0.20 |

Fresh ignitions (< 30%) are positive in all 4 cells. ≥ 30% only works in SR1, where the tail (BAKE +21R, MYX +14R) sits.
This fits the original hypothesis ("catch it in the first hour, before it is a stale top mover") and the tester idea
"cap change_24h at about 30-40%". It answers the researcher's diagnostic question ("does R increase with |change_24h|?"): no,
it decreases. The cost is SR1's tail; ex-top-3 the < 30% subset is still -0.28R (SR1) / +0.03R (SR2) on the market version, so
it remains a tail strategy.

Ideas checked and rejected on SR evidence:
- **Early failure exit** (tester idea): for trades still open at 30/60/90/120 min and below entry, the final R
  (mkt SR1 -0.05/+0.13/-0.04/-0.15; SR2 -0.31/-0.23/-0.33/-0.31) is the same as or better than exiting at that point
  (-0.47/-0.47/-0.38/-0.37; -0.30/-0.25/-0.36/-0.32). Same for the ladder version. Not added.
- **Funding filter** (researcher #9): the last settled funding rate at the signal (from `data/funding`) splits the
  opposite way in the two windows (mkt SR1: f ≥ 0 +0.83R vs f < 0 -0.12R; SR2: f ≥ 0 -0.22R vs f < 0 +0.16R). Not added, and
  no engine change was made.
- **Half market + half limit rung** (tester idea): the risk manager makes every rung of a ladder either a limit or market
  (`Rung(p if ladder else None)`), so this needs an engine change. Not done this round.
- **Wider initial stop** (tester idea for ignition_r2): the evidence for it was the lookahead stop-distance cut. Not added.

### Round 3 configs

| Config | Idea | Hypothesis being tested |
|---|---|---|
| `ignition_mkt_r3` | `ignition_mkt_r2` + `max_change: 0.30`: skip signals when \|change_24h\| ≥ 30% at the signal candle. Everything else unchanged (market entry, longs only, no signals 18-24 UTC, trail 3 ATR5 after +1R, time stop 600). | The edge is in *fresh* movers. Capping change_24h at 30% removes the SR2 losers (≥ 30%: -0.41R price-only) and makes SR2 price gross clearly positive without relying on funding. Pre-registered: if SR2 price gross is ≤ 0 or SR1+SR2 gross avg R ≤ +0.05, the cap is rejected. Expect ~0.9 trades/day (38 research trades). |
| `ignition_r3` | `ignition_r2` (limit ladder at -0.5/-1.0 ATR5) + `max_change: 0.30`. | The ladder twin of `ignition_mkt_r3`. Ladder fills were cheaper but missed the runaway tail. With the cap, which is better per signal? The < 30% subset was +0.04R / +0.88R price-only on the ladder. |
| `ignition_mkt_fresh_r3` | `ignition_mkt_r3` with a looser trigger: 1h return ≥ 4% (was 6%) and last-hour volume ≥ 3x (was 4x). | The cap leaves a small sample. If the edge is "fresh mover + attention shock" and not the exact thresholds, loosening the trigger inside the cap adds trades at a similar gross R per trade. If gross R per trade falls well below `ignition_mkt_r3`, the 6%/4x trigger matters. Not tuned on any data (new signals). |

Implementation notes:
- New param `max_change` in `scalp_ignition.py` (default 0 = off, so all earlier ignition configs are unchanged). Checked
  before the daily cap, so a skipped signal does not use up the symbol's one entry per day.
- Implementation check (SR1 only): `ignition_mkt_r3` gives exactly the 21 SR1 trades of the `ignition_mkt_r2` subset with
  change_24h < 30%.
- Only 3 configs this round: every other surviving idea was dropped by the kill rule, and I didn't find a new researcher
  idea with a better prior than the remaining candidates (`sigma_fade` is a fade control with a low prior; `funding_filter`
  has no SR support, see above).
- Smoke test (2025-09-01..02): all 3 run. `uv run pytest -q` passes. No engine code was changed.

Testers: report price gross, fees and funding separately (as in round 2), the ex-top-3 numbers per window, the market vs
ladder R per signal and fill rate for `ignition_mkt_r3` / `ignition_r3`, and a change_24h split (12-20 / 20-30%) to see if
the edge sits in one bucket. Don't cut on the CSV `stop` column (see warning above).

## Holdout pre-registration

Written 2026-11-13, before any holdout (SH1/SH2) run. The finalists were chosen only from research (SR1/SR2) and
validation (SV1/SV2) results (`reports/research/<name>/results.json` / `summary.txt`) plus the tester reports.

Pre-set selection criteria: positive return in ≥ 3 of the 4 SR/SV windows, combined SR+SV profit factor ≥ 1.1,
trade-weighted avg R > 0 in both research and validation, ≥ 100 trades in total. Combined PF is gross profit / gross loss
summed over the 4 windows, rebuilt from each window's net PnL and PF.

Four configs meet the criteria: `ignition_mkt_r2`, `ignition_mkt_fresh_r3`, `ignition_r2` and `vwap_pullback_r2`.
`vwap_pullback_r2` is not a finalist. It meets the criteria on paper, but the SR1+SR2 price gross is +0.045R per trade
(at the kill line), SR2 is profitable only because of funding, PF 1.06/1.03 in two windows is noise, and it was dropped in
round 3 by the kill rule. Consistency and simplicity win over a fourth slot.

### Finalists (all longs-only, no signals 18-24 UTC, trail 3 ATR5 after +1R, 600-min time stop)

| Rank | Config | SR1 | SR2 | SV1 | SV2 | Windows + | Comb. PF | SR avg R | SV avg R | Trades |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `ignition_mkt_r2` (market entry, no change cap) | +46.1% / 64 / PF 2.29 / +0.65R | +3.2% / 43 / 1.16 / +0.08R | +12.0% / 50 / 1.57 / +0.24R | +5.5% / 51 / 1.23 / +0.11R | 4/4 | 1.66 | +0.418 | +0.173 | 208 |
| 2 | `ignition_mkt_fresh_r3` (market, change_24h < 30%, 1h ≥ 4%, vol ≥ 3x) | +11.0% / 29 / 1.75 / +0.39R | +18.1% / 27 / 2.57 / +0.63R | +19.5% / 22 / 3.18 / +0.84R | -1.8% / 40 / 0.92 / -0.04R | 3/4 | 1.79 | +0.504 | +0.275 | 118 |
| 3 | `ignition_r2` (limit ladder -0.5/-1.0 ATR5, no change cap) | +2.5% / 52 / 1.10 / +0.11R | +6.8% / 32 / 1.47 / +0.20R | +7.2% / 35 / 1.57 / +0.41R | -4.4% / 34 / 0.70 / -0.12R | 3/4 | 1.18 | +0.147 | +0.150 | 153 |

Why this order: `ignition_mkt_r2` is the only design positive in all 4 windows, has the largest sample and has no
parameter chosen on SR trade cuts. Its weaknesses are that SR2 price gross is about 0 (the SR2 profit is funding) and that
the result depends on the tail (SR1 top 3 = +40R of +41R). `ignition_mkt_fresh_r3` has the best combined PF and its SR2
profit comes from price, but the 30% cap was chosen on SR trades (in-sample) and it fails SV2. `ignition_r2` is the cheaper
ladder twin. It has positive price gross in both SR windows, but a lower PF, and it misses the runaway tail.

Caveats, stated before the run:
- All three share the same ignition signal, so the holdout results are strongly correlated and do not count as three
  independent tests.
- All three depend on the tail: without the top 3 trades every SR window is about 0R or negative. A holdout with no
  +10R runner is likely to lose even if the idea is sound.

**Holdout success criterion (per finalist):** net return > 0 in **both** SH1 (2026-08-17..09-06) and SH2
(2026-10-19..11-08), with configs unchanged and the cost model unchanged. Anything else is a failure. No re-tuning after the
run. Each config is run once.

### Ranking of all tested configs (SR + SV only)

| Rank | Config | Windows + | Comb. PF | SR avg R | SV avg R | Trades | Meets criteria |
|---|---|---|---|---|---|---|---|
| 1 | ignition_mkt_r2 | 4/4 | 1.66 | +0.418 | +0.173 | 208 | yes (finalist) |
| 2 | ignition_mkt_fresh_r3 | 3/4 | 1.79 | +0.504 | +0.275 | 118 | yes (finalist) |
| 3 | ignition_r2 | 3/4 | 1.18 | +0.147 | +0.150 | 153 | yes (finalist) |
| 4 | vwap_pullback_r2 | 4/4 | 1.14 | +0.065 | +0.053 | 327 | yes (not picked: kill rule, funding-only SR2) |
| 5 | ignition_mkt_r3 | 3/4 | 1.99 | +0.697 | +0.175 | 70 | no (< 100 trades) |
| 6 | ignition_r3 | 3/4 | 1.64 | +0.414 | +0.410 | 52 | no (< 100 trades) |
| 7 | xs_intraday | 3/4 | 1.17 | -0.011 | +0.110 | 507 | no (SR avg R < 0) |
| 8 | coil_break_r2 | 2/4 | 1.85 | +0.445 | +0.322 | 125 | no (2/4, one-coin SR1) |
| 9 | vwap_pullback | 3/4 | 1.05 | +0.034 | +0.105 | 357 | no (PF) |
| 10 | coil_break | 2/4 | 1.17 | +0.160 | -0.025 | 237 | no |
| 11 | ignition | 2/4 | 1.08 | +0.013 | +0.178 | 195 | no |
| 12 | vwap_pullback_mkt_r2 | 2/4 | 1.02 | -0.028 | +0.035 | 266 | no |
| 13 | base_confluence | 3/4 | 0.97 | +0.159 | -0.152 | 24 | no |
| 14 | session_orb | 1/4 | 1.00 | -0.068 | +0.084 | 78 | no |
| 15 | flush_catch | 1/4 | 0.83 | +0.015 | -0.016 | 280 | no |
| 16 | vwap_fail_short | 0/4 | 0.50 | -0.171 | -0.272 | 34 | no |
| 17 | base_scalper | 0/4 | 0.76 | -0.160 | -0.133 | 1787 | no (negative control) |

No holdout window has been run.

## Holdout results

Run 2026-11-13, once per finalist, configs and cost model unchanged:
`uv run autotr research-eval --config config/research/scalp/<name>.yaml --only-holdout --workers 4 --out reports/research/<name>_holdout`.
Afterwards each holdout window was also re-run with `autotr backtest` on the same dates (same config, no changes; the
trade counts match the research-eval output exactly) to get the trades.csv with price gross, fees and funding
(`reports/research/<name>_holdout/trades_SH{1,2}/`). These are diagnostics only. Nothing was re-tuned.

Gross = price PnL before fees and funding; funding is income here (longs on movers mostly *received* funding).
Price R = gross / risk per trade. Ex-top-3 = avg R without the 3 best trades. Start balance $100 per window.

| Config | Window | Return | Trades | /day | Win % | PF | Avg R | Price R | Gross / fees / funding ($) | Best trade | Ex-top-3 R |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `ignition_mkt_r2` | SH1 | **-15.7%** | 50 | 2.4 | 30% | 0.49 | -0.336 | -0.349 | -16.27 / 0.59 / +1.18 | +4.3R | -0.53 |
| | SH2 | **-20.6%** | 49 | 2.3 | 24% | 0.31 | -0.470 | -0.471 | -20.73 / 0.53 / +0.62 | +2.5R | -0.63 |
| `ignition_mkt_fresh_r3` | SH1 | **-7.7%** | 26 | 1.2 | 38% | 0.46 | -0.306 | -0.293 | -7.40 / 0.43 / +0.11 | +1.7R | -0.52 |
| | SH2 | **-8.0%** | 13 | 0.6 | 23% | 0.12 | -0.640 | -0.660 | -8.23 / 0.25 / +0.51 | +0.5R | -0.94 |
| `ignition_r2` | SH1 | **-8.8%** | 32 | 1.5 | 38% | 0.45 | -0.287 | -0.298 | -9.03 / 0.31 / +0.54 | +2.8R | -0.52 |
| | SH2 | **-13.0%** | 34 | 1.6 | 21% | 0.43 | -0.449 | -0.447 | -12.93 / 0.32 / +0.26 | +3.6R | -0.76 |

**Verdict: all three finalists fail the pre-registered criterion (net return > 0 in both SH1 and SH2). 0 of 6 holdout
windows are positive. No scalping strategy passed.**

Diagnostics (holdout, no decisions taken from them):
- **The loss is in the signal, not in fees.** Fees were 0.25-0.59$ per window against -7 to -21$ of price loss; funding
  was a small plus. The price R per trade (-0.29 to -0.66R) is about as bad as a coin flip with a 1R stop and almost no winners.
- **No tail.** SR1 was carried by +21R / +14R runners. In the holdout the best trade was +4.3R and the top 3 trades
  together made +1 to +8R; without them every window is -0.5 to -0.9R per trade. This is exactly the failure mode named in
  the pre-registration caveat ("a holdout with no +10R runner is likely to lose even if the idea is sound"), but the
  non-tail trades were also much worse than in SR/SV (ex-top-3 about 0R there, -0.5R or worse here).
- **The change_24h < 30% cap did not help.** In `ignition_mkt_r2` the < 30% trades were -0.45R (SH1, n16) and -0.65R
  (SH2, n8) price-only, worse than the ≥ 30% trades (-0.30 / -0.44R). The SR "fresh mover" split was noise.
  `ignition_mkt_fresh_r3` lost less money only because it traded less.
- **Ladder adverse selection again.** `ignition_r2` filled both rungs on 41 of 66 trades: when it fills deep, the move
  is failing. Its smaller loss vs the market twin is fewer, cheaper trades, not a better signal.
- Exits: 91-97% of trades ended on the (initial or trailed) stop; few reached the 10h time stop.

## Conclusion

**No winner.** Over 3 rounds, 17 configurations and 7 strategy families, nothing produced a scalping edge on the daily
top movers that survived the locked holdout. The finalists were positive in 4/4 or 3/4 research + validation windows,
then lost 8-21% in each holdout window (PF 0.12-0.49). The SR/SV results were a few lucky runners plus selection: 17 configs and several SR cuts
were tried on 4 windows, so the best of them looking good on those 4 windows was expected even with no edge.

### What was learned per family

| Family | Best config | Result | Lesson |
|---|---|---|---|
| Market-wide 1m scalper / 5m confluence (baselines) | `base_scalper` | -36% to -57% per window | many trades; fees and no edge. Same as the momentum search. |
| Flush catch (limit bids under a capitulation candle) | `flush_catch` | PF 0.66-1.02 | deep limit rungs fill on the trades that keep falling (adverse selection). |
| VWAP pullback (limit buy on a dip in an uptrending mover) | `vwap_pullback_r2` | PF 1.03-1.28, SR price gross +0.045R | the closest thing to break-even. Profit was mostly funding. Not a finalist, no holdout. |
| VWAP failure short | `vwap_fail_short` | 0/4 windows | shorting movers after a VWAP loss loses; too few signals. |
| Session opening-range breakout | `session_orb` | 1/4 windows | no gross edge. |
| Coil break (tight range, then breakout) | `coil_break_r2` | SR1 +37% (3 MYX trades), SR2/SV2 negative | one-coin result. |
| Cross-sectional intraday momentum among movers (4h rebalance) | `xs_intraday` | SR gross -0.006R | the 4h/7-day cross-sectional edge (`config/momentum.yaml`) does not carry down to intraday holds inside the mover set. |
| Ignition (fresh 1h impulse + volume shock, trail) | `ignition_mkt_r2` | SR/SV PF 1.16-2.29, **holdout PF 0.31-0.49** | it looked like the one surviving idea, but the SR/SV profit was a few runners. The holdout had none, so it lost. |

Cross-cutting lessons:
- **Costs were solved, the signal was not.** With ≥ 2.5% stops and ≤ 2-3 trades a day, fees fell to 0.02-0.05R per
  trade (from 60-90% of equity in the old 1m/5m systems). The losses that remain are price losses.
- **Buying strength in top movers is a tail bet.** Most entries are stopped out. The profit depends on whether the window
  has one or two +10-20R runaway coins. Three weeks are too short to average that out, so the window results look like noise.
- **SR trade cuts don't generalise.** Every split chosen on research trades (change_24h cap, hour filter, side filter)
  either flipped or vanished out of sample. Watch out also for lookahead: the CSV `stop` column is the final stop (Round 3).
- **Limit ladders on movers are adversely selected.** The rungs fill on the losers and miss the runners. Cheaper fills
  do not make up for that.
- Compared with the momentum search: long holds (12-48h) across a stable universe carried an edge. Minute-to-hour holds
  on movers did not.

### Limits of this test
- **Simulated fills on 1m OHLC bars.** No order book and no sub-minute ordering. Stop-first when stop and target share a
  candle (pessimistic), but market entries at the next open and stops at the stop price + 5 bps are *optimistic* for
  small, fast coins. Real slippage on a +40% mover during an impulse can be several times 5 bps, so live results would be
  worse, not better.
- **Universe construction.** Top-20 movers by |daily change| with ≥ $20M volume, data from 2 days before to 1 day after;
  the coin pool is picked by UTC-day change, then the scanner ranks it by rolling 24h change. Live, the pool is instead
  the 150 highest-volume perps (`live_candidates`). Close but not identical.
- **Few, short windows.** 6 windows of 3 weeks, 13-64 trades per window. A tail-driven strategy needs many more trades to
  tell skill from luck. The holdout result is clear (0/6 windows, all PF < 0.5), but the earlier positives were never
  statistically meaningful.
- **Correlated finalists.** All three finalists used the same signal, so this was one holdout test, not three.
- **The holdout is used up.** Any new scalping idea now needs fresh data (paper trading forward from 2026-11-13, or
  windows after 2026-11-08) for an honest test.

`config/scalp.yaml` is `ignition_mkt_r2` (the pre-registered #1, not chosen on holdout numbers) set up for live paper
trading, labelled as unvalidated. It is there to watch the signal forward on the dashboard, not to make money.
