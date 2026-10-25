# Pre-registration: multi-ETF channel breakout (v1)

Registered 2026-10-25, before any result for these rules exists. Tag: `prereg-etf-breakout-v1`.
Owner decision 2026-10-25: "approved. Proceed with ... multi-ETF daily trend registration." Research only; NO-GO
for paper or live trading under this registration. Three variants, frozen before testing; only the base is judged.

Binding copy of the rules: `aether-research/etf_breakout/eb_rules.py` (module docstring and constants). If this
document and the code disagree, the code at the registration tag is what was registered, and the disagreement is
recorded in strategy.md rather than "fixed".

## 1. Hypothesis

Across five liquid US ETFs from different asset classes (US large caps, US tech, US small caps, gold, long
Treasuries), buying a new 55-day closing high and selling a new 20-day closing low earns more, per unit of risk,
than holding the same ETFs and than the 200-day filter AETHER already tested, after costs.

Source: the classic channel-breakout trend rule (Donchian; the 55/20 pair is the published "Turtle" system).
A textbook rule is a hypothesis source, not evidence. The parameters are taken as published, not tuned.

## 2. Why this is genuinely distinct from what AETHER has already tested

| | S2 (frozen) | daily-v1 S-A (retired) | This registration |
|---|---|---|---|
| Question | which ETFs are strongest *relative to each other* | is each ETF above its 200-day average | did each ETF just make a *new 55-day high* |
| Type | cross-sectional momentum, top 3 of 10 | slow trend *filter* (state) | trend *breakout* (event) with a separate, faster exit |
| Decision | monthly | daily | daily |
| Entry / exit | rank-based | same line both ways | asymmetric: 55-day high in, 20-day low out |
| Universe | 10 ETFs + SHY | 6 ETFs + SHY | 5 ETFs, idle sleeves in cash |

S-A is used here as a **comparator** (criterion C8): if the breakout cannot beat the 200-day filter on the same
ETFs with the same mechanics, it adds nothing new and is not pursued.

## 3. Rules (summary of eb_rules.py)

- **Universe:** SPY, QQQ, IWM, GLD, TLT. Five sleeves, each starting with 1/5 of the capital, never rebalanced
  against each other. A sleeve is either fully in its ETF or in cash at 0% (conservative: no interest is credited).
- **Entry (flat sleeve):** close of day t > highest close of the previous 55 days.
- **Exit (long sleeve):** close of day t < lowest close of the previous 20 days.
- **Execution:** at the OPEN of day t+1. Signals use only closes up to day t (no look-ahead; tested).
- **Long only.** No stops, targets, leverage or pyramiding. Sleeves start flat on 2007-03-01; a sleeve enters only
  on a new breakout.
- **Variants (exactly three, frozen):** base `eb_55_20`; neighbours `eb_20_10` and `eb_100_50` for robustness only.
- **Costs:** 5 bp per side on the sleeve value at every entry and exit (covers spread and slippage on these ETFs,
  whose spreads are about 1 cent; no commission at Alpaca). Stress: 10 bp per side (C7).

## 4. Data

- `aether-research/us_momentum/data/us_daily_yf.csv.gz`, Yahoo daily, dividend- and split-adjusted, SHA-256
  `e9524a2a60e1c609af72c991d1be8cc8d42f36d95bcafcf1b1b81dd1971ed9b6` (the file S2 used; S14 showed it agrees with
  Alpaca's adjusted bars, worst median difference 1.5 bp). No new fetch. Open and close only.
- Window: warm-up from 2005-01-03; evaluation 2007-03-01 to 2026-10-06 (4,932 trading days, including 2008,
  2011, 2020 and 2022). Coverage checked before registration: all five symbols have all 5,474 days 2005-01-03 to
  2026-10-06; any gap refuses the run (no filling).
- **Disclosed prior exposure.** These ETFs and this file were used for S2 and S-A. No channel-breakout rule has
  been run on them, and Claude computed no return statistic of these series while writing this registration (only
  the coverage counts above). The 2007-2026 path of these markets is widely known, so this is not an independent
  experiment; N in section 7 is the full registry.
- **Out of sample.** Nothing is fitted (published parameters), so the whole window is scored with frozen rules.
  Robustness is tested by halves (C5: 2007-2015 vs 2016-2026), by year (C6) and by neighbours (C12). The forward
  period after 2026-10-06 is the true out-of-sample test (section 8).

## 5. Correlation between the ETFs

SPY, QQQ and IWM move together, so five sleeves are not five independent bets. This is handled by:
- judging the **portfolio's daily returns** (Sharpe, drawdown, bootstrap), never pooling trades as if independent;
- a **block bootstrap** (21-day blocks, 5,000 draws, seed 0) for the Sharpe confidence interval, which keeps
  cross-ETF and serial dependence inside each block;
- **C11:** no single ETF may supply more than 50% of the profit;
- reporting the average pairwise correlation and the effective number of independent bets,
  5 / (1 + 4 x average correlation).

## 6. Measures, benchmarks and dollars

Base and each neighbour: trades, win rate, average win and loss, profit factor, Sharpe with block-bootstrap 95%
interval, CAGR, max drawdown, total return, Sharpe in each half, calendar-year returns, CAGR at 10 bp/side,
deflated Sharpe, trades and profit share per ETF.

Comparators (same five sleeves, same open execution, same costs): the 200-day filter (C8) and buy-and-hold (C9).
Reported only: SPY buy-and-hold, and 60% SPY / 40% TLT rebalanced monthly.

**Dollars** at $100, $1,000 and $5,000: average dollars per trading day and per year, worst full year and
maximum drawdown in dollars, and the capital that the historical average would need for $2 per trading day. These
are descriptions of the past, not forecasts. At $100 each sleeve is $20; Alpaca's fractional shares make that
tradable, but the dollar amounts are small by construction.

## 7. Criteria (base variant; all fixed now)

| | Criterion | Pass if |
|---|---|---|
| C1 | Enough trades | >= 100 closed trades across the five ETFs |
| C2 | Edge with uncertainty | CAGR > 0 **and** block-bootstrap 95% lower bound of Sharpe > 0 |
| C3 | Economic size | trade profit factor >= 1.30 |
| C4 | Drawdown limit | max drawdown of daily portfolio returns <= 20% |
| C5 | Not one era | Sharpe > 0 in both 2007-03-01..2015-12-31 and 2016-01-01..2026-10-06 |
| C6 | Not one lucky stretch | positive in >= 11 of the 18 full years 2008-2025 |
| C7 | Survives worse fills | CAGR > 0 at 10 bp/side |
| C8 | Adds something new | Sharpe > the 200-day filter's on the same five ETFs |
| C9 | Worth more than holding | Sharpe >= buy-and-hold of the same five ETFs |
| C10 | Not luck among many tries | deflated Sharpe >= 0.95 (necessary, never sufficient) |
| C11 | Not one ETF | no ETF > 50% of the profit |
| C12 | Not a lucky parameter | both neighbours have CAGR > 0 |

**Trial count for C10.** N = max(49, logged trials + backfilled + 3 outside the database + these three if not yet
logged). 49 = 46 in the registry on 2026-10-25 + these 3. Trial Sharpe variance: the same surviving record every
registration uses (0.000324276 per day).

**PBO** across the three variants is reported only.

## 8. Verdicts (fixed now) and what follows

- **Historically unsupported:** base CAGR <= 0. Retired; no re-tuning.
- **Historically supported:** C1-C12 all pass.
- **Insufficient evidence:** fewer than 100 trades and not unsupported.
- **Mixed:** anything else.

A changed lookback, universe, cost, sizing or cash rule is a new strategy with its own registration and trials.

**Only if historically supported:** a separate PR proposes a forward journal (frozen rules replayed daily on new
data, no orders), as for trend-pullback. Paper trading would need its own PR and an explicit owner approval;
nothing is built for either before the verdict.

## 9. What no verdict can do

A historical result is research evidence only. It does not override S2's NO-GO, `docs/AETHER_TEST_PLAN.md`,
the Risk Controller, the halt file, the Stage 6 and S2 soak requirements or the Oct 22 decision, and it permits no
paper or live order. `LIVE_TRADING` stays false.

## 10. Run procedure (server, in this order)

1. Merge this PR, then on the merged commit: `git tag prereg-etf-breakout-v1 && git push origin prereg-etf-breakout-v1`.
2. `.venv/bin/python aether-research/etf_breakout/eb_hist.py run` (refused unless the tag is an ancestor of HEAD,
   tracked files are committed and the data hash matches).
3. `.venv/bin/python aether-research/etf_breakout/eb_hist.py run --log-trials` (logs the three variants once).
4. Archive the results file in the repo, record the verdict in strategy.md and in the strategy registry.
