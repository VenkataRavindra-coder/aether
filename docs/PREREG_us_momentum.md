# Pre-registration: US ETF momentum rule (plan item S1)

Status: DRAFT until committed. It must be committed BEFORE any strategy-truth result (S2 to S20) is produced.
Companion to `docs/AETHER_TEST_PLAN.md` (tag `plan-v2.5` or later). Where this file and the plan disagree, the
plan's criteria win; this file only fixes what the plan leaves open.

## 1. The rule under test

`us_strategy.py` exactly as committed at registration (fingerprint in section 10). Once a month rank ten ETFs
(SPY, QQQ, IWM, EFA, EEM, IEF, TLT, GLD, DBC, VNQ) by the average of their 3, 6 and 12 month returns
(63/126/252 trading days), hold the top three at equal weight, and replace any of the three whose score is not
positive with SHY. Signals use closes strictly before the first trading day of the month. No part of the rule is
changed during the evaluation. A change to the rule is a NEW rule with its own pre-registration and its own trial.

## 2. Data

- Alpaca daily bars, dividend and split adjusted (`us_momentum/data/us_daily_all.csv.gz`), about 2016 onward.
- Yahoo daily bars via yfinance, auto-adjusted (`us_momentum/data/us_daily_yf.csv.gz`), from about 2006.
- S14 decides whether the two sources agree (median daily-return difference under 5 bps for every symbol, and at
  least 95% of monthly selections identical over the overlap). If S14 passes, the longer Yahoo history is the
  primary sample for S2, S3 and S6 to S13, because 2008 and 2011 exist only there. If S14 fails, the Alpaca sample
  is primary and S12 is reported as "2008 and 2011 not testable".
- Both cache files are fingerprinted with SHA-256 in every result file.

## 3. Execution model

Baseline: rebalance at the OPEN of the first trading day of the month, 2 bps slippage per side, zero commission,
SEC and FINRA sell fees added as published. Cost stress (S5) multiplies slippage by 2 and 3.

Known deviation: the live engine rebalances after 15:30 ET on the first trading day, not at the open. A second
variant executes at the first day's CLOSE. It is reported under S9 (time shift) and S20; it does not carry its
own pass or fail.

## 4. What counts as a trial (the N in the deflated Sharpe)

N counts every strategy attempted in the search for something to deploy, whether or not it was kept: the four
logged trials plus the 24 backfilled estimates in `untracked_trials` (28 in total), and any further strategy
variant that is considered for selection. This evaluation selects nothing: the rule is fixed, so it adds no
trials to N.

The S6 parameter neighbourhood and every other robustness run is logged in a separate table (`research_runs`)
with `purpose = robustness`, never in `research_trials`, so it cannot inflate or shrink N. Re-running an
identical configuration does not create a new trial; each configuration is identified by a hash of its
parameters, the code fingerprint and the data fingerprint.

## 5. The PBO family (S3)

Every combination of lookbacks {50, 63, 76} x {101, 126, 151} x {202, 252, 302} and top-N {2, 3, 4}: 81 variants,
the base rule among them. Daily return series of all 81 are cut into 16 contiguous blocks (CSCV). The base rule
is deployed; the family is used to ask whether the in-sample winner of such a search would hold up out of sample.

## 6. Holdout

The 2022-onward period is declared USED: the research code names it a holdout (`--split-date 2022-01-01`,
`holdout_2022_on`) but it has been examined repeatedly, so it cannot serve as a single-use holdout.

The single-use holdout (S4) is all data AFTER 2026-10-07, the date plan v2.5 locked. It is evaluated once, at the
end of the soak, and the evaluation is recorded in `docs/holdout.lock` (date, code fingerprint, result). A second
evaluation is refused by the research code. A few monthly rebalances cannot establish significance, so S4 is a
consistency check against the simulation; the statistical weight rests on S2, S3 and S5 to S13 over the long history.

## 7. Seeds and procedure

Every random procedure uses seed 20261007 (bootstrap 5,000 draws of 21-day blocks; Monte Carlo drawdown 10,000
permutations; placebo 1,000 random-signal strategies; shuffled-returns null 1,000 shuffles). Results are written to
`us_momentum/results/` with the fingerprints of code and data.

## 8. Stated expectation (written before any result)

Preview from the trial table alone (OOS Sharpe 0.83 over 2022 to 2026, N = 28, spread of trial Sharpes 0.29):
the deflated Sharpe is about 0.70 on the 2022+ window and 0.78 to 0.91 over 10 to 30 years of history. A Sharpe of
about 1.1 (10 years) or 0.96 (20 years) would be needed to reach 0.95. **The S2 criterion is therefore expected to
FAIL for this rule.** If it does, the result is recorded as FAIL and the verdict for "edge established" is NO-GO.
Nothing in this file or in the plan changes after the result is seen.

## 9. If the verdict is NO-GO

The plan has no waiver. Going to micro-live anyway is a separate, explicit decision by the owner, recorded in
`strategy.md` as a RISK ACCEPTANCE (not a pass): the maximum dollar amount, the reason ("learning exercise on a
published rule; edge not established"), a sunset date, and a stop rule. Paper trading continues unchanged either way.

## 10. Fingerprints at registration (2026-10-07)
```
7bad288a43b99eb4a2478fc34fa8a1acc9c280c1d1b673e1a7b95301122c5b43  us_strategy.py
a2566918c0831c72a6ffedf70f77a92942cb1c7c99d5f6f207cc087d246f4814  aether-research/research_gate.py
b96580de8858a14b8bfa7951968c390b2b5f2b9008da669060ce180277d6633e  aether-research/us_momentum/long_history.py
be40ac2bc37d43909e889fdb00ccabfa82b81fd12996a9e78e7be2f26ad74dc7  aether-research/us_momentum/us_momentum_backtest.py
4ce1f6f07e7507dca567610359cff6d61e58d6dd430fead8703fbf5aef380081  aether-research/us_momentum/data/us_daily_all.csv.gz
```
