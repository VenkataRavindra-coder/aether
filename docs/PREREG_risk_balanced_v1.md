# Pre-registration: risk-balanced ETF holding (v1)

Registered 2026-10-25, before any result for these rules exists. Tag: `prereg-risk-balanced-v1`.
Owner decision 2026-10-25: register risk-balanced holding first, then the 200-day filter with T-bills as a separate
strategy; parameters pre-registered, realistic costs, every variant counted, no promotion on historical returns
alone. Research only; NO-GO for paper or live under this registration.

Binding copy of the rules: `aether-research/risk_balanced/rb_rules.py`. If this document and the code disagree,
the code at the registration tag is what was registered, and the disagreement is recorded in strategy.md.

## 1. Hypothesis

Holding SPY, QQQ, IWM, GLD and TLT all the time, with each weighted by the inverse of its recent volatility and
rebalanced monthly, gives better risk-adjusted returns than holding them in equal weights, and better net returns
than the 200-day filter on the same ETFs without substantially more drawdown.

Source: inverse-volatility ("naive risk parity") weighting, a textbook allocation rule. A hypothesis source, not
evidence. It was chosen after etf-breakout-v1 because both timing rules tested so far lost return by sitting in cash;
this rule never sits in cash, and it weights the calmer assets (bonds, gold) more, which also addresses the QQQ
concentration seen in etf-breakout-v1. That motivation is disclosed: the idea was picked knowing those results.

## 2. Rules (summary of rb_rules.py)

- **Universe:** SPY, QQQ, IWM, GLD, TLT; always fully invested.
- **Weights:** w = (1/sigma) / sum(1/sigma), sigma = sample standard deviation of the last 63 daily close-to-close
  returns ending at the close before the rebalance day (no look-ahead; tested).
- **Rebalance:** at the OPEN of the first trading day of each month, and at the first open of the window. Holdings
  drift between rebalances.
- **Costs:** 5 bp per side on traded notional (sum of |target - current| at the open); the first purchase pays on
  the whole portfolio. Stress: 10 bp per side (C7).
- **Variants (exactly three, frozen):** base `rb_63`; neighbours `rb_21` and `rb_126` (robustness, C12). All three
  are logged as trials.

## 3. Data

The same hash-pinned file as etf-breakout-v1 and S2: `aether-research/us_momentum/data/us_daily_yf.csv.gz`,
SHA-256 `e9524a2a60e1c609af72c991d1be8cc8d42f36d95bcafcf1b1b81dd1971ed9b6`, loaded by etf-breakout-v1's loader
(refuses any gap). Evaluation 2007-03-01 to 2026-10-06 (4,932 days), warm-up from 2005-01-03.

**Disclosed prior exposure.** These ETFs and this file were used for S2, S-A and etf-breakout-v1, whose results are
known, including the buy-and-hold and 200-day filter figures used as comparators here. Claude has not computed any
inverse-volatility or equal-weight-monthly result on them. Not an independent experiment; N is the full registry.
Nothing is fitted; robustness is checked by halves (C5), years (C6) and neighbours (C12). The true out-of-sample
test is the period after 2026-10-06.

## 4. Comparators

Built with the same code, costs and open execution:
- **Equal weight, monthly** (1/5 each, same schedule): isolates whether the *weighting* adds anything (C2, C3, C5).
- **200-day filter on the same five ETFs**: etf-breakout-v1's registered comparator, unchanged (C8-C10).
- Reported only: buy-and-hold of the five (never rebalanced), SPY buy-and-hold, 60/40 SPY/TLT.

## 5. Measures and dollars

Sharpe with block-bootstrap 95% interval (21-day blocks, 5,000 draws, seed 0), CAGR, max drawdown, total return,
Sharpe in each half for strategy and equal weight, calendar years, CAGR at 10 bp/side, average monthly turnover,
average and latest weights, deflated Sharpe, and the paired block-bootstrap 95% interval of
Sharpe(strategy) - Sharpe(equal weight), resampling the same days for both. Dollars at $100, $1,000 and $5,000 and
the capital the historical average would need for $2 per trading day (descriptions of the past, not forecasts).

## 6. Criteria (base variant; all fixed now)

| | Criterion | Pass if |
|---|---|---|
| C1 | Positive with uncertainty | CAGR > 0 **and** block-bootstrap 95% lower bound of Sharpe > 0 |
| C2 | Weighting helps | Sharpe > equal weight's |
| C3 | ... beyond noise | paired bootstrap 95% lower bound of (Sharpe - equal-weight Sharpe) > 0 |
| C4 | Drawdown limit | max drawdown <= 20% |
| C5 | Not one era | Sharpe > equal weight's in both 2007-2015 and 2016-2026 |
| C6 | Not one lucky stretch | positive in >= 11 of 18 full years 2008-2025 |
| C7 | Survives worse fills | CAGR > 0 at 10 bp/side |
| C8 | More net return than the filter | CAGR > the 200-day filter's |
| C9 | Not worse risk-adjusted | Sharpe >= the 200-day filter's |
| C10 | Not substantially more risk | max drawdown <= 1.25 x the 200-day filter's |
| C11 | Not luck among many tries | deflated Sharpe >= 0.95 (necessary, never sufficient) |
| C12 | Not a lucky parameter | both neighbours' Sharpe > equal weight's |

**N** = max(52, logged + backfilled + 3 outside + these three if not yet logged); 52 = 49 + 3. Trial Sharpe
variance: the same surviving record as every registration (0.000324276 per day). PBO across the three: reported
only.

## 7. Verdicts (fixed now)

- **Historically unsupported:** CAGR <= 0, or Sharpe <= equal weight's (the weighting adds nothing).
- **Historically supported:** C1-C12 all pass.
- **Mixed:** anything else.

A changed lookback, universe, schedule, cost or weighting is a new strategy with its own registration and trials.

## 8. What no verdict can do

Research evidence only. It does not override S2's NO-GO, `docs/AETHER_TEST_PLAN.md`, the Risk Controller, the halt
file, Stage 6, the S2 soak or the Oct 22 decision, and it permits no paper or live order. Only "historically
supported" allows a separate forward-journal PR (no orders); paper and live each need their own approval.

## 9. Run procedure (server, in this order)

1. Merge this PR; on the merged commit `git tag prereg-risk-balanced-v1 && git push origin prereg-risk-balanced-v1`.
2. `.venv/bin/python aether-research/risk_balanced/rb_hist.py run --log-trials` (refused unless the tag is an
   ancestor of HEAD, tracked files are committed and the data hash matches; logs the three variants once).
3. Archive the results file in the repo; record the verdict in strategy.md and the registry.
