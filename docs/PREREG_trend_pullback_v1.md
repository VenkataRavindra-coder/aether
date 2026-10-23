# Pre-registration: SPY trend-pullback candlestick signals (v1)

Registered 2026-10-23, before any result for these rules exists. Tag: `prereg-trend-pullback-v1`.
Owner decision 2026-10-23: "GO for the research PR. NO-GO for live trading." Exactly four variants, frozen before
testing, no parameter optimisation and no discretionary change after results are seen.

Binding copy of the rules: `aether-research/trend_pullback/rules.py` (module docstring and constants). If this
document and the code ever disagree, the code at the registration tag is what was registered, and the
disagreement is recorded in strategy.md rather than "fixed".

## 1. Hypothesis

In SPY, when price is in an uptrend (close above EMA50) and pulls back to EMA20, a bullish engulfing or hammer
candle followed by a break of that candle's high marks a better-than-random long entry, after costs.

Source: a shared candlestick write-up (2026-10-23) ranked "trend + pullback" first. That is a hypothesis source,
not evidence: published candlestick studies on liquid US equities mostly find no reliable edge after costs.

## 2. The four variants (all logged, winners and losers)

| Variant | Timeframe | Pattern |
|---|---|---|
| `tpb_engulf_1h` | 1 hour | bullish engulfing |
| `tpb_hammer_1h` | 1 hour | hammer |
| `tpb_engulf_1d` | daily | bullish engulfing |
| `tpb_hammer_1d` | daily | hammer |

No neighbours, no parameter sweeps. Each variant is judged on its own; none replaces another after the run.

## 3. Rules (summary of rules.py)

**Candles.** Built from regular-hours 1-minute bars.
- 1 hour: full hours from the open (09:30-10:30 ... 14:30-15:30). The 15:30-16:00 tail (12:30-13:00 on early
  closes) is never a signal or entry slot and does not feed the EMAs; positions are held through it and can exit
  in it. An hour needs >= 54 of its 60 minutes.
- Daily: one candle per session, needs >= 90% of the session's minutes.
- A missing candle is skipped by the EMAs. Engulfing needs its two candles in consecutive slots.

**EMA.** On candle closes, continuous across days: alpha = 2/(n+1), seeded with the simple average of the first n
closes. EMA20 and EMA50. No signal before candle index 150 (warm-up).

**Signal** on a completed candle: close > EMA50 (trend), low <= EMA20 (pullback touches or crosses), and the pattern:
- Bullish engulfing: previous candle red, this one green, open <= previous close, close >= previous open, body
  strictly larger than the previous body.
- Hammer: range > 0; body <= 0.35 x range; lower wick >= 2 x body and >= 0.60 x range; upper wick <= 0.10 x range;
  either colour.

**Entry.** Only during the next slot (for 1 hour after 14:30-15:30, that is the next session's first hour). The
first minute whose high is above the signal high triggers; fill = max(that minute's open, signal high), so a gap
above the trigger fills at the worse open. No trigger in that slot: the signal expires.

**Stop, target, exits.** Stop = signal low. Risk R = fill - stop. Target = fill + 2R. On the entry minute only the
stop is checked. Then per minute: low <= stop exits at min(open, stop); else high >= target exits at max(open,
target); stop first if both. Overnight and weekend gaps fill at the next open by the same rule.
Maximum hold: end of the 6th hourly slot after the entry slot (1 hour), or of the 10th session after the entry
session (daily); exit at the close of the last minute before that time.

**One position at a time** per variant. Long only. Whole sleeve per trade (no leverage, no sizing rule here).

**Costs.** 5 bp per side (10 bp round trip). Stress: 10 bp per side.

## 4. Data

- The SPY SIP 1-minute file already fetched for `tactical-hist-v1` (2016-01-04 to 2026-10-20), raw prices,
  Alpaca market calendar. No new fetch. The run refuses unless the file's SHA-256 matches the one recorded in
  strategy.md (`1ca901b4...5ee745`). Nothing on or after 2026-10-21 is read.
- Sessions that fail `aether_tactical.bars.verify` contribute no minutes; more than 2% failing refuses the run.
- **Disclosed prior exposure.** This file has been used for the ORB and 15-minute engulfing tests (both
  unsupported), and SPY's 2016-2026 path is widely known. No trend-pullback rule has been run on it. This is not an
  independent experiment, which is why the trial count in section 6 is the full registry.
- **Raw prices.** Dividends are not added back: ex-dividend days show as small drops for both the strategy and
  buy-and-hold. This slightly understates long returns and is accepted.
- **Out of sample.** Nothing is fitted, so the whole window is scored with frozen rules. Chronological robustness
  is tested by halves (criterion C5) and by year (C6). The forward journal (section 7) is the true out-of-sample.

## 5. Measures and benchmarks

Per variant: trades, win rate, mean net bp per trade, t-stat, bootstrap 95% interval of the mean (10,000
resamples of trades, seed 0), profit factor, daily mark-to-market returns (whole sleeve, costs on entry and exit
days), total return, max drawdown, annualised Sharpe, calendar-year returns, exit reasons. The evaluation window of
each timeframe starts at the session of its 150th candle.

- **Buy-and-hold:** SPY over the same evaluation window (open of the first day, then close to close).
- **Matched random entries:** the null pool is every candle that passes warm-up and the trend filter (close >
  EMA50) with the same entry, stop, target and hold mechanics, but no pullback and no pattern. Each of 1,000 runs
  (seed 20261023) draws the variant's number of trades per signal year from that year's pool, without replacement.
  p = (1 + runs with mean >= real mean) / 1,001. This asks whether the pullback and pattern add anything beyond
  "buy any breakout in an uptrend".

## 6. Criteria (each variant, all fixed now)

| | Criterion | Pass if |
|---|---|---|
| C1 | Enough trades | >= 100 |
| C2 | Edge after costs, with uncertainty | mean net bp > 0 at 5 bp/side **and** bootstrap 95% lower bound > 0 |
| C3 | Economic size | profit factor >= 1.20 |
| C4 | Drawdown limit | max drawdown of daily mark-to-market <= 20% |
| C5 | Not one era | mean net bp > 0 in both halves (signals before / from 2021-01-01) |
| C6 | Not one lucky stretch | positive in >= 60% (rounded up) of full calendar years in the window, up to 2025 |
| C7 | Survives worse fills | mean net bp > 0 at 10 bp/side |
| C8 | Better than random | matched random-entry p <= 0.05 |
| C9 | Worth more than holding | Sharpe >= buy-and-hold Sharpe over the same window |
| C10 | Not luck among many tries | deflated Sharpe >= 0.95 (necessary, never sufficient) |

**Trial count for C10.** N = max(44, logged trials + backfilled + 3 attempts outside the database + these four if
not yet logged). On 2026-10-23 that is 9 logged + 28 backfilled + 3 + 4 = 44 (`docs/TRIAL_INVENTORY_daily_v1.md`;
its UNKNOWN items are still not counted as zero in spirit). Trial Sharpe variance: the same surviving record
daily-v1 and tactical-hist-v1 use. The four are not treated as an independent experiment.

**PBO** is computed across the four variants and **reported only**. With four variants on two different
timeframes it is unstable and is not used for any decision.

## 7. Verdicts and rejection (fixed now)

- **Historically unsupported:** mean net bp <= 0 at 5 bp/side. The variant is retired. No re-tuning.
- **Historically supported:** C1-C10 all pass.
- **Insufficient evidence:** fewer than 100 trades and not unsupported. Daily variants may well end here; that is
  an accepted outcome, not a reason to change rules or add symbols.
- **Mixed:** anything else.

A changed rule, timeframe, threshold, symbol or cost is a new strategy: it needs its own registration and counts
toward N. QQQ or GLD can only come later, as a new registration, and only if a SPY variant is supported.

**Forward journal (all four variants, whatever the historical verdict).** From the first session after
registration (2026-10-26) a timer replays the frozen rules on IEX 1-minute data each weekday at 16:25 New York and
appends each closed trade to `aether-research/trend_pullback/forward_data/journal.jsonl` (append-only; a replay
that disagrees with a stored row is written as a mismatch and the run fails). It uses the **second paper
account's** data keys (`TP_APCA_API_KEY_ID`, `TP_APCA_API_SECRET_KEY`) and refuses the main paper or live keys.
It places **no orders**: order-placing paper execution on the second account would be a separate PR, proposed only
for a historically supported variant. Collection runs until 30 forward trades per variant or 2027-10-26; the
forward state is then "consistent" (mean net bp > 0) or "not consistent". IEX here vs SIP in history is a known
difference.

## 8. What no verdict can do

A historical or forward result is research evidence only. It does not:
- override S2's NO-GO, `docs/AETHER_TEST_PLAN.md` (v2.1), the Risk Controller, the halt file, the tactical live
  gate or the paper-soak restrictions;
- change the Oct 22 decision (no live strategy; benchmark allocation for capital; canary off);
- permit any live or paper order.

The most a "historically supported" verdict permits is that a separate, new registration may be proposed for an
order-placing paper test on the second paper account. Live trading would additionally need forward evidence, the
test plan's go/no-go, the Risk Controller and canary path, and an explicit owner decision.

## 9. Run procedure (server, in this order)

1. Merge this PR, then on the merged commit: `git tag prereg-trend-pullback-v1 && git push origin
   prereg-trend-pullback-v1` (before any run).
2. `python3 aether-research/trend_pullback/hist.py run` (refused unless the tag is an ancestor of HEAD, tracked
   files are committed and the data hash matches).
3. `python3 aether-research/trend_pullback/hist.py run --log-trials` (logs the four variants once; refused if
   batch `trend-pullback-v1` already exists).
4. Record all four verdicts in strategy.md. No re-runs with different settings.
5. Forward journal: add the second paper account's keys to `.env` as `TP_APCA_API_KEY_ID` /
   `TP_APCA_API_SECRET_KEY`, install `deploy/aether-trendpb-forward.{service,timer}` and enable the timer.
