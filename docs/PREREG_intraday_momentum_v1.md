# Pre-registration: SPY intraday momentum (v1)

Registered 2026-10-25, before any result for these rules exists. Tag: `prereg-intraday-momentum-v1`.
Owner decision 2026-10-25: GO for the research PR (owner left the details to Claude). NO-GO for live trading.
Exactly two variants, frozen before testing, no parameter optimisation and no discretionary change after results.

Binding copy of the rules: `aether-research/intraday_momentum/im_rules.py` (module docstring and constants). If
this document and the code ever disagree, the code at the registration tag is what was registered, and the
disagreement is recorded in strategy.md rather than "fixed".

## 1. Hypothesis

On SPY, the return from the previous session's close to 10:00 New York predicts the direction of the last half
hour of the same session, by enough to pay costs.

Source: Gao, Han, Li and Zhou, "Market intraday momentum", Journal of Financial Economics (2018). The proposed
mechanism is late-day trading by investors who rebalance or hedge near the close (for example leveraged-ETF
rebalancing and option hedging), plus late-informed traders. A published anomaly is a hypothesis source, not
evidence. Known risks, stated now: the effect may have decayed after publication, and the average move in the
last half hour is a few basis points, so 10 bp round-trip costs may consume all of it.

Why it fits AETHER: one decision per day, a known signal time (10:00) and a known trade time (15:30). Speed
does not matter at this horizon, which suits a Hetzner server trading through Alpaca.

## 2. The two variants (both logged, winners and losers)

| Variant | Rule |
|---|---|
| `imom_long` | long the last half hour if r1 > 0, otherwise no trade |
| `imom_ls` | long if r1 > 0, short if r1 < 0 (research only: the live account has shorting disabled) |

No thresholds, no neighbours, no other symbols. Each variant is judged on its own.

## 3. Rules (summary of im_rules.py)

- **r1** = close of the 1-minute bar starting at open + 29 min (09:59, closing at 10:00) / previous session's
  last close - 1. The overnight gap is included, as in the source.
- **Entry** at the OPEN of the bar starting at close - 30 min (15:30; 12:30 on early closes).
- **Exit** at the CLOSE of the session's last bar (must start in the final 5 minutes).
- The previous session's last bar must also start in its final 5 minutes. A missing signal bar, entry bar or
  final bar, or a failed session (today or yesterday), means no trade that day.
- Flat overnight, at most one trade per day, whole sleeve, no stop, no target, no leverage.
- **Costs** 5 bp per side (10 bp round trip). Stress: 10 bp per side.
- No look-ahead: the signal is final at 10:00 and the trade happens 5.5 hours later.

## 4. Data

- The SPY SIP 1-minute file already fetched for `tactical-hist-v1` (2016-01-04 to 2026-10-20), raw prices, Alpaca
  market calendar. No new fetch. The run refuses unless the file's SHA-256 matches the one recorded in strategy.md
  (`1ca901b4...5ee745`). Nothing on or after 2026-10-21 is read.
- Sessions that fail `aether_tactical.bars.verify` contribute no trades; more than 2% failing refuses the run.
- **Disclosed prior exposure.** This file has been used for ORB, 15-minute engulfing and (registered) the
  trend-pullback rules. No last-half-hour rule has been run on it, and Claude has not computed any statistic of
  SPY's last half hour on this file before registering. The anomaly itself is public, so this is not an
  independent experiment; that is why N in section 6 is the full registry.
- **Out of sample.** Nothing is fitted, so the whole window is scored with frozen rules. Robustness is tested by
  halves (C5) and by year (C6). The 2016-2026 window is entirely after the source paper's sample (1993-2013), so
  it is out of sample with respect to the publication.

## 5. Measures and benchmarks

Per variant: trades, win rate, mean gross and net bp, t-stat, bootstrap 95% interval of the mean (10,000
resamples, seed 0), profit factor, daily returns (net trade return on trade days, 0 otherwise), total return,
max drawdown, annualised Sharpe, calendar-year returns.

- **Buy-and-hold:** SPY close to close over the same window.
- **Matched random days:** each of 1,000 runs (seed 20261025) draws the variant's number of trades per calendar
  year from that year's eligible days, without replacement, and takes the last-half-hour return with the same
  costs (for `imom_ls`, each drawn day also gets a random side). p = (1 + runs with mean >= real mean) / 1,001.
  This asks whether the 10:00 signal adds anything beyond "trade the last half hour on any day".

## 6. Criteria (each variant, all fixed now)

| | Criterion | Pass if |
|---|---|---|
| C1 | Enough trades | >= 100 |
| C2 | Edge after costs, with uncertainty | mean net bp > 0 at 5 bp/side **and** bootstrap 95% lower bound > 0 |
| C3 | Economic size | profit factor >= 1.20 |
| C4 | Drawdown limit | max drawdown of daily returns <= 20% |
| C5 | Not one era | mean net bp > 0 in both halves (trades before / from 2021-01-01) |
| C6 | Not one lucky stretch | positive in >= 6 of the 10 full years 2016-2025 |
| C7 | Survives worse fills | mean net bp > 0 at 10 bp/side |
| C8 | Better than random | matched random-day p <= 0.05 |
| C9 | Worth more than holding | Sharpe >= buy-and-hold Sharpe over the same window |
| C10 | Not luck among many tries | deflated Sharpe >= 0.95 (necessary, never sufficient) |

**Trial count for C10.** N = max(46, logged trials + backfilled + 3 attempts outside the database + these two if
not yet logged). 46 = the 44 of trend-pullback-v1 (its four variants included whether or not they have run) +
these 2. Trial Sharpe variance: the same surviving record daily-v1, tactical-hist-v1 and trend-pullback-v1 use.

**PBO** across two variants is not meaningful; it is reported only (or reported as an error) and never used.

## 7. Verdicts and rejection (fixed now)

- **Historically unsupported:** mean net bp <= 0 at 5 bp/side. The variant is retired. No re-tuning.
- **Historically supported:** C1-C10 all pass.
- **Insufficient evidence:** fewer than 100 trades and not unsupported.
- **Mixed:** anything else.

A changed signal time, entry time, threshold, symbol (QQQ, IWM, ...) or cost is a new strategy: it needs its own
registration and counts toward N. QQQ can only come later, as a new registration, and only if a SPY variant is
supported.

**Next step if, and only if, `imom_long` is historically supported:** a separate PR proposes automatic
order-placing paper trading of `imom_long` on the second (stress-test) paper account, with its own forward
verdict rule. Nothing is built for that before the historical verdict exists.

## 8. What no verdict can do

A historical result is research evidence only. It does not:
- override S2's NO-GO, `docs/AETHER_TEST_PLAN.md` (v2.1), the Risk Controller, the halt file, the tactical live
  gate or the paper-soak restrictions;
- change the Oct 22 decision (no live strategy; benchmark allocation for capital; canary off);
- permit any live or paper order.

Live trading would additionally need forward paper evidence, the test plan's go/no-go, the Risk Controller and
canary path, and an explicit owner decision.

## 9. Run procedure (server, in this order)

1. Merge this PR, then on the merged commit: `git tag prereg-intraday-momentum-v1 && git push origin
   prereg-intraday-momentum-v1` (before any run).
2. `.venv/bin/python aether-research/intraday_momentum/im_hist.py run` (refused unless the tag is an ancestor of
   HEAD, tracked files are committed and the data hash matches).
3. `.venv/bin/python aether-research/intraday_momentum/im_hist.py run --log-trials` (logs the two variants once;
   refused if batch `intraday-momentum-v1` already exists).
4. Record both verdicts in strategy.md. No re-runs with different settings.
