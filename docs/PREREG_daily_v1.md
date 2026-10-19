# Pre-registration: three daily-frequency US ETF candidates (S-A, S-B, S-C)

Status: LOCKED at the commit tagged `prereg-daily-v1` (the tag is created on the merge commit of the pull request
that adds this file). No result for S-A, S-B or S-C may be produced before that tag exists. Registered 2026-10-19.

Companion to `docs/AETHER_TEST_PLAN.md` and `docs/PREREG_us_momentum.md`. Where this file and the plan disagree, the
plan's criteria win; this file only fixes what the plan leaves open. "S-A/S-B/S-C" are names for this document only
and are unrelated to plan items S1 to S20.

This document authorizes research only. It does not authorize live orders, does not change the VTI execution
canary, and changes no engine code (`US_LIVE` stays false).

## 1. Purpose and honesty notes

Three long-only US ETF rules that act on a daily clock are tested to see whether any deserves a paper soak. They
are textbook ideas (trend filter, short-term mean reversion, turn-of-month seasonal). Pre-registration here means
the rules, parameters, costs, tests and verdict labels are fixed before a result exists. It does not mean the
ideas are unseen: the author knew them in advance, and the 2022-onward period has been examined repeatedly for
other rules. The forward period in section 8 is the only evidence that is not contaminated.

**Earlier related experiment, disclosed.** `aether-research/us_momentum/daily_edges_backtest.py` (committed
2026-10-03) already defines a mean-reversion rule (SPY and QQQ: 3 consecutive lower closes, close above the 200-day
average, exit above the 5-day average or after 5 sessions) and a turn-of-month rule (SPY, close of the last
trading day to the close of the 3rd). S-B and S-C are close relatives of those rules. Whether that script was ever
run, and what it logged, is recorded in `research.sqlite` on the server and could not be checked when this file
was written. Section 7 therefore counts trials from the database at run time, and the first step of the run is to
print that count.

## 2. Data

- Primary: `aether-research/us_momentum/data/us_daily_yf.csv.gz` (Yahoo daily open and close, auto-adjusted for
  splits and dividends, 2005-01-03 to 2026-10-06; the same file S2 used). SHA-256
  `e9524a2a60e1c609af72c991d1be8cc8d42f36d95bcafcf1b1b81dd1971ed9b6`. The S14 agreement result already recorded for
  this file carries over. A different fingerprint at run time stops the run.
- Symbols used: SPY, QQQ, IWM, EFA, TLT, GLD, IEF, SHY.
- Evaluation window: 2007-03-01 to 2026-10-06 (the S2 window). Earlier bars are used only as warm-up for averages.
- A missing bar for a symbol on a day it is needed is a data defect: the run stops and reports it. No forward
  fill, no interpolation.
- The runner truncates all data at 2026-10-06. Nothing after that date is read before section 8 allows it.

## 3. Execution and cost model (all three candidates)

- **Order time:** 09:45 ET on the session after the signal, never before 09:45 (the first-15-minutes rule).
  Marketable limit orders, as the engine uses.
- **Price used in the simulation:** the open of that session, adjusted. This is a proxy: daily bars contain no
  09:45 price. A sensitivity run using Alpaca (IEX) 5-minute bars at 09:45 may be added where that history exists
  (about 2016 onward). It is reported only and carries no pass or fail.
- **Return accounting:** a position decided at the open of session d earns the adjusted open(d) to open(d+1)
  return of the asset held. Costs are charged at the open where the trade happens.
- **Costs:** 5 bp of traded notional per side on every trade including SHY, plus a sell-side regulatory fee of
  0.3 bp of sale value (the figure in `daily_edges_backtest.py`). Cost stress at 2x and 3x slippage is reported only.
- **Cash:** the idle sleeve is held in SHY and earns SHY's actual return, with the costs above on every switch.
- **Start:** each candidate starts fully in SHY with notional 1.0 on 2007-03-01 (the first open in the window).
- **Ties:** a close exactly equal to its average is "not above". An RSI exactly equal to the threshold is "not below".
- **Averages:** simple moving averages of adjusted closes, including the signal day.
- **Calendar:** NYSE trading days. Half-days are trading days.

## 4. The three rules (one fixed parameter set each, no tuning)

### S-A: daily trend filter
- Six equal sleeves (1/6 each): SPY, QQQ, IWM, EFA, TLT, GLD.
- At each close, a sleeve's state is ON if its close is above its 200-day SMA, else OFF. ON means the sleeve holds
  that ETF, OFF means it holds SHY. A state change at close of day t is executed at 09:45 ET of day t+1.
- On the first trading day of each month the six sleeves are reset to equal weight at the same execution time.
  Between resets sleeves drift.
- **Own comparator:** the same six ETFs, 1/6 each, never in SHY, reset on the same monthly schedule, same costs.
- **Round trip:** an ON period of one sleeve, from entry to exit.

### S-B: short-term mean reversion
- Instrument: SPY only; everything else is SHY.
- RSI is Wilder's RSI with period 2 on adjusted closes (smoothing factor 1/2, seeded by the simple average of the
  first two changes, with at least 200 bars of warm-up so the seed has no effect).
- **Entry:** at the close of day t, if RSI(2) < 10 and close > SMA(200), buy SPY (all of the sleeve) at 09:45 ET of
  day t+1. Day t+1 is session 1 of the holding period. One position at most.
- **Exit:** at each close from session 1 onward, if close > SMA(5), or the position has completed 5 sessions
  (the close of session 5), sell at 09:45 ET of the next session. So the longest hold is 5 full sessions.
- **Round trip:** entry to exit.

### S-C: turn-of-month seasonal (TLT)
- **Explicitly my hypothesis.** The window was proposed by Claude, not taken from the thread that inspired it, and
  that thread could not be checked when this was written.
- Long only (shorting is disabled on the live account).
- **Entry:** 09:45 ET on the last trading day of each month: buy TLT with the whole sleeve.
- **Exit:** 09:45 ET on the 4th trading day of the next month: sell TLT, buy SHY.
- Held through the closes of the last trading day and trading days 1, 2 and 3, i.e. four open-to-open intervals,
  which includes the third trading day without trading in the first 15 minutes. SHY at all other times.
- The 4th trading day is counted with the first trading day of the new month as day 1.
- **Round trip:** one window.

## 5. Benchmarks

- **Passive benchmark P:** 60% SPY and 40% IEF, reset to target weights at 09:45 of the first trading day of each
  month, same costs, same window and start. It is the passive 60/40 used in S17, with a monthly reset instead of
  S17's daily approximation.
- **SPY buy-and-hold:** one purchase at the first open of the window, same costs, never rebalanced.
- Both are reported for every candidate. Criterion 4 (section 6) compares against both.

## 6. The four criteria, applied to every candidate

All four apply to each of S-A, S-B and S-C. Each is reported even if an earlier one failed. No short-circuit.

1. **Deflated Sharpe ratio >= 0.95.** `research_gate.deflated_sharpe` on the candidate's daily net return series
   over the full evaluation window (annualised with 252, raw kurtosis), with N as set in section 7. `dsr_report.py` is
   run as a cross-check and any difference is reported.
2. **PBO <= 0.05.** `research_gate.pbo_cscv`, 16 contiguous blocks, up to 20,000 combinations, seed 20261007, on
   the daily net returns of the candidate's 9-variant family below over the days all nine share. The pre-registered
   rule is one member of the family and is the one deployed if it passes. Family members are logged in
   `research_runs` with `purpose = robustness`, never as trials.
   - S-A: SMA length {150, 200, 250} x confirmation days {1, 3, 5} (a state changes only after that many
     consecutive closes on the other side). Base 200 and 1.
   - S-B: RSI(2) threshold {5, 10, 15} x exit SMA length {3, 5, 10}. Base 10 and 5. The 200-day trend filter and
     the 5-session limit are fixed.
   - S-C: entry day {second-to-last trading day of the month, last trading day, first trading day of the next
     month} x exit on trading day {3, 4, 5} of the next month. Base last day and 4.
3. **Monte Carlo drawdown within 27%.** As S11: the candidate's monthly net returns are re-ordered at random
   10,000 times, seed 20261007, and the 95th percentile of the maximum drawdown must be <= 27%
   (`research_gate.mc_max_drawdown`). The observed daily maximum drawdown is reported.
4. **Beats the passive benchmark after costs.** Over the identical window, net Sharpe **and** net Calmar are both
   higher than P's and higher than SPY buy-and-hold's. This is stricter than S17, which accepts Sharpe **or**
   Calmar; both readings are reported. CAGR and market exposure (share of days not in SHY) are reported beside it.

**Candidate falsifiers (each is a FAIL if hit):**
- S-A: observed daily maximum drawdown is not lower than its own comparator's, or net Sharpe is not higher than
  the comparator's.
- S-B: net expectancy per round trip is zero or negative after costs.
- S-C: the random-day test below gives p > 0.05.

**S-C random-day test.** Under the null, the 4-interval holding window starts on a random day. For each of 10,000
draws (seed 20261007) and for every calendar month in the window, one start day is drawn uniformly from the trading
days of that month that have a complete 4-interval window inside the sample. The statistic is the mean, over the
months, of TLT's compounded open-to-open return over the window minus SHY's over the same intervals, gross of costs.
The real statistic comes from the registered windows. One-sided p = (1 + number of null draws at least as large
as the real one) / (1 + draws). Required: p <= 0.05.

**Three outcomes for each criterion and falsifier, never merged:**
- **PASS**: computed on a sufficient sample and met.
- **FAIL**: computed on a sufficient sample and not met.
- **INSUFFICIENT**: not computable or below these minimums: fewer than 30 round trips, fewer than 756 daily
  returns, fewer than 60 monthly returns, fewer than 320 common days for PBO (16 blocks x 20), fewer than 60 windows
  for the S-C random-day test, a NaN or non-finite result, or a data defect. INSUFFICIENT is never counted as PASS
  or FAIL, and never turned into one by changing the rule.

**Verdict per candidate:**
- **WORTH PAPER-TESTING**: all four criteria and the candidate's falsifier are PASS.
- **UNSUPPORTED**: at least one FAIL. Final for that rule. A changed rule is a new candidate with its own
  registration and its own trial.
- **INCONCLUSIVE**: no FAIL, at least one INSUFFICIENT. The only remedy is more data under the identical rule;
  no rule edit.

WORTH PAPER-TESTING means a paper soak may be designed. It is not a go for live. Phase 2 of `strategy.md` section 8
still needs its own gates and a funded, preflighted account. How a candidate would be paper-tested is decided after
the verdict, and an engine code change for it may restart the soak clock under check A4.

## 7. Trial counting (the N in the deflated Sharpe)

The previous history is not reset. At run time N is computed by the existing method:

N = (rows in `research_trials`) + (sum of `untracked_trials.count`) + (any of S-A, S-B, S-C not yet logged)

The floor is 31: the 28 recorded for S2 plus these three. If `daily_edges_backtest.py` logged trials that are not
already inside the 28, they are in the database counts and N is higher. Rules:

- N is 31 or more from the moment this file is committed, whether or not all three candidates are ever run.
- Dropping a failed or unrun candidate never lowers N. Any later candidate raises it, and the DSR is recomputed on
  the final N when a gate decision is made.
- The trial Sharpe variance is the variance of the per-period Sharpe ratios of the logged trials, as `dsr_report.py`
  computes it, including the three new trials once logged.
- Each candidate is logged once with `trials.log_trial`, including its full daily return series, identified by a
  hash of parameters, code fingerprint and data fingerprint. Re-running an identical configuration is not a new trial.
- The S2 record in `strategy.md` is unchanged (N was 28 then, verdict NO-GO). A DSR for S2 recomputed on the larger
  N could only be lower.

## 8. Holdout

The single-use holdout is all data after 2026-10-07, the same forward period as S2's S4 holdout. It stays closed:

- The runner may not read any bar after 2026-10-06, and refuses to open the holdout until this registration is on
  `main` and the soak has completed.
- Each candidate is evaluated on it once, recorded in `docs/holdout.lock` (date, code fingerprint, result). A second
  evaluation is refused by the research code.
- Criterion: net return > 0 and annualised Sharpe >= 0.5 x that candidate's in-sample Sharpe, with no tolerance band.
- INSUFFICIENT if there are fewer than 5 completed round trips (S-A, S-B) or fewer than 6 completed windows (S-C).
- It is a forward consistency check that can only falsify. Passing it does not by itself promote anything.
- S2 and these candidates share the period, so the number of holdout evaluations is reported with every result.

## 9. Seeds, procedure and records

- Every random procedure uses seed 20261007.
- Reported only, no pass or fail: 21-day block bootstrap Sharpe (5,000 draws), cost stress 2x and 3x, the 09:45
  five-minute sensitivity, yearly profit concentration, exposure and turnover.
- Each result file records: the git tag `prereg-daily-v1` and the commit hash it points to, the runner's SHA-256,
  the data SHA-256, the fingerprints in section 10, and N with its breakdown. Results without these are invalid.
- The runner is written and committed after this registration and before any run. Its fingerprint is added to the
  result, not to this file.

## 10. Fingerprints at registration (2026-10-19)
```
656df59d84cdf45ae928f5fd27bab637d188999cd19ec8f0f6446d75a8d7ae27  aether-research/research_gate.py
5fe9883e1f502c036da3e666ebf54fcf9ab5509565648f54c58d090264cc9aee  aether-research/dsr_report.py
036390daec8f46b7571eebf651acf5d9861a9c5f6b9e282f31cf190d10be8f50  aether-research/pbo.py
3f32373add074e5da3c7530ae358a1cf9165a26b3536b57822756e71bf3415d8  aether-research/trials.py
f527091ae7c1bbe0e90d9566ad3df62ca05b597d149423ea4d116df176100d88  aether-research/us_momentum/strategy_truth.py
e9524a2a60e1c609af72c991d1be8cc8d42f36d95bcafcf1b1b81dd1971ed9b6  aether-research/us_momentum/data/us_daily_yf.csv.gz
```
Base commit at registration: `58e9acb428e5533a64e63a6ff63f7f4120f31a71`.
