# Pre-registration: 200-day filter with idle money in T-bills (v1)

Registered 2026-10-25, before any result for these rules exists and before the BIL data is downloaded.
Tag: `prereg-sma-tbill-v1` (created after the BIL data is committed, section 9). Owner decision 2026-10-25: register
the 200-day filter with idle capital in short-term T-bills as a separate strategy, using BIL. Research only; NO-GO for
paper or live under this registration.

Binding copy of the rules: `aether-research/sma_tbill/st_rules.py`. If this document and the code disagree, the code at
the registration tag is what was registered, and the disagreement is recorded in strategy.md.

## 1. Hypothesis

The 200-day filter on SPY, QQQ, IWM, GLD and TLT already cut drawdowns (16.5% vs 29.4% holding) but gave up return
(7.8% vs 11.3% a year), partly because idle money earned nothing. Holding 1-3 month Treasury bills (BIL) instead of
cash when an ETF is below its 200-day average raises net return without adding market risk, and the result is at
least as good as holding the five ETFs on a risk-adjusted basis.

This is cash management, not a new signal: the filter itself is unchanged. It is registered because a real account
would hold T-bills, not 0% cash, and the honest comparison with holding needs that.

## 2. Rules (summary of st_rules.py)

- Five sleeves of 1/5, never rebalanced against each other. Each holds its ETF while its close is above its 200-day
  simple average (including that day), else BIL. A change at close t is executed at the open of t+1 (sell one, buy
  the other). The first day starts in whatever state the previous close gave.
- Costs: 5 bp per side on every sale and purchase, BIL included (so a switch costs 10 bp). Stress 10 bp per side.
- Variants (exactly three, frozen): base `st_200`; neighbours `st_150` and `st_250` (robustness, C11). All logged.
- Comparator built by the same code: the same rule with idle cash at 0%. With N = 200 it reproduces etf-breakout-v1's
  registered 200-day comparator exactly (tested).

## 3. Data

- ETFs: the registered file `aether-research/us_momentum/data/us_daily_yf.csv.gz`, SHA-256
  `e9524a2a60e1c609af72c991d1be8cc8d42f36d95bcafcf1b1b81dd1971ed9b6` (etf-breakout-v1 loader, refuses gaps).
- BIL: not yet downloaded. `fetch_bil.py fetch` downloads it once (Yahoo via yfinance, adjusted), 2007-05-30 (inception)
  to 2026-10-06 (the ETF file's last day), into `aether-research/sma_tbill/data/bil_daily_yf.csv.gz`, and writes its
  SHA-256 to `bil_manifest.json`. The fetch prints only dates and counts. Both files are committed and the tag is
  created on that commit, so the data is frozen before any result; the runner refuses if the file does not match the
  manifest at the tag, or if BIL misses any trading day of the window.
- Window: 2007-06-01 (first full month after BIL's inception) to 2026-10-06; warm-up from 2005-01-03 for the averages.
  This is three months shorter than etf-breakout-v1's window, so its comparator figures are recomputed here.
- **Disclosed prior exposure.** The 200-day filter's results on these ETFs (with cash) and S-A's (with SHY) are known.
  No T-bill version has been run, and no BIL return has been looked at. Not an independent experiment; N is the full
  registry. Nothing is fitted.
- **Expected by construction:** C2, C3 and C5 should pass whenever T-bills pay more than 0% and switches are rare;
  they confirm the accounting, not an edge. The informative criteria are C8 to C10.

## 4. Comparators

Same five ETFs, same open execution and costs: the idle-cash version of each variant (C2, C3, C5, C11) and
buy-and-hold of the five, never rebalanced (C8, C9). Reported only: risk-balanced-v1's `rb_63` rule on the same window,
BIL held alone, SPY buy-and-hold, 60/40 SPY/TLT.

## 5. Measures and dollars

Sharpe with block-bootstrap 95% interval (21-day blocks, 5,000 draws, seed 0), CAGR, max drawdown, total return, CAGR in
each half for both versions, calendar years, CAGR at 10 bp/side, switches per ETF, deflated Sharpe. Dollars at $100,
$1,000 and $5,000, and the capital the historical average would need for $2 per trading day (past, not forecasts).

## 6. Criteria (base variant; all fixed now)

| | Criterion | Pass if |
|---|---|---|
| C1 | Positive with uncertainty | CAGR > 0 **and** block-bootstrap 95% lower bound of Sharpe > 0 |
| C2 | T-bills add net return | CAGR > the idle-cash version's |
| C3 | ... without worse risk-adjusted return | Sharpe >= the idle-cash version's |
| C4 | Drawdown limit | max drawdown <= 20% |
| C5 | Not one era | CAGR > the idle-cash version's in both 2007-2015 and 2016-2026 |
| C6 | Not one lucky stretch | positive in >= 11 of 18 full years 2008-2025 |
| C7 | Survives worse fills | CAGR > 0 at 10 bp/side |
| C8 | Worth more than holding | Sharpe >= buy-and-hold of the same five ETFs |
| C9 | Less risk than holding | max drawdown <= buy-and-hold's |
| C10 | Not luck among many tries | deflated Sharpe >= 0.95 (necessary, never sufficient) |
| C11 | Not a lucky parameter | both neighbours' CAGR > their idle-cash versions' |

**N** = max(55, logged + backfilled + 3 outside + these three if not yet logged); 55 = 52 + 3. Trial Sharpe variance:
the same surviving record as every registration (0.000324276 per day). PBO across the three: reported only.

## 7. Verdicts (fixed now)

- **Historically unsupported:** CAGR <= 0, or CAGR <= the idle-cash version's (T-bills add nothing).
- **Historically supported:** C1-C11 all pass.
- **Mixed:** anything else.

A changed average length, idle asset, universe, cost or execution is a new strategy with its own registration.

## 8. What no verdict can do

Research evidence only. It does not override S2's NO-GO, `docs/AETHER_TEST_PLAN.md`, the Risk Controller, the halt
file, Stage 6, the S2 soak or the Oct 22 decision, and it permits no paper or live order. Only "historically supported"
allows a separate forward-journal PR (no orders); paper and live each need their own approval.

## 9. Run procedure (server, in this order)

1. Merge this PR and `git pull`.
2. One-time research environment for the download (keeps yfinance out of the engine's environment):
   `python3 -m venv ~/research-venv && ~/research-venv/bin/pip install yfinance pandas`
3. `~/research-venv/bin/python aether-research/sma_tbill/fetch_bil.py fetch` (refuses if the file exists).
4. `git add -f aether-research/sma_tbill/data/bil_daily_yf.csv.gz aether-research/sma_tbill/data/bil_manifest.json`,
   commit and push.
5. On that commit: `git tag prereg-sma-tbill-v1 && git push origin prereg-sma-tbill-v1`.
6. `.venv/bin/python aether-research/sma_tbill/st_hist.py run --log-trials` (refused unless the tag is an ancestor of
   HEAD, the BIL files are in the tagged commit and both hashes match; logs the three variants once).
7. Archive the results file; record the verdict in strategy.md and the registry.
