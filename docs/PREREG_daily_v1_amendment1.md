# Amendment 1 to `docs/PREREG_daily_v1.md` (candidates S-A, S-B, S-C)

Status: LOCKED at the commit tagged `prereg-daily-v1.1` (created on the merge commit of the pull request that adds
this file). Written 2026-10-19, before any result for S-A, S-B or S-C exists: the runner's `run` command has been
locked (`RUN_LOCKED`) since #35 and nothing has been executed on the real history. An amendment made before any result
is allowed; one made after a result is not. Where this file and the original disagree, this file wins; everything not
mentioned here is unchanged.

## 1. Why

Section 3 of the original uses the session OPEN (09:30 ET) as a stand-in for the 09:45 ET order time. The open falls in
the first 15 minutes, the very period the rules exclude, so the stand-in can change fills, returns and verdicts. It
was disclosed in the original but not made a pass condition. This amendment fixes that before any result.

## 2. Two bases, both required

**Basis 1: open-proxy (unchanged).** The 2007-03-01 to 2026-10-06 run in the original sections 3 to 6, used for the
long history that DSR, PBO and the Monte Carlo drawdown need (it includes 2008). It is a proxy and is labeled that way
in every result. On its own it can never produce WORTH PAPER-TESTING.

**Basis 2: 09:45 ET fills (new, gating).** A fifth criterion applied to every candidate:

5. **Execution-price robustness.** Over the window where Alpaca IEX five-minute bars exist for all eight symbols
   (SPY, QQQ, IWM, EFA, TLT, GLD, IEF, SHY), each candidate and both benchmarks are re-run with every fill at the 09:45
   ET price instead of the open. PASS requires all of:
   - net Sharpe > 0, and >= 0.75 x the open-proxy Sharpe over the identical window (if that Sharpe is positive);
   - net CAGR > 0;
   - criterion 4 still holds (Sharpe AND Calmar above 60/40 and SPY), with the benchmarks also filled at 09:45.

   FAIL if the sample is sufficient and any of these is not met. INSUFFICIENT if the overlap has fewer than 756 daily
   returns or fewer than 30 completed round trips (the original minimums), or if more than 2% of all required fills are
   substituted fills (see the fill convention). The verdict rules are unchanged, so a FAIL here makes the candidate UNSUPPORTED and an INSUFFICIENT makes it
   INCONCLUSIVE.

**Criterion 5 is mandatory.** WORTH PAPER-TESTING needs all five criteria present and PASS. A run without criterion 5
(not implemented, intraday data missing, overlap too short, fallback budget exceeded) can only end INCONCLUSIVE or
UNSUPPORTED, never WORTH PAPER-TESTING; the runner's `verdict` enforces this and has a test for it.

**Fill convention (Basis 2).** The fill price is the adjusted OPEN of the five-minute bar timestamped 09:45 ET (the first
IEX print from 09:45:00), `Adjustment.ALL`, IEX feed. If that bar is missing, use the open of the 09:50 bar, then the
09:55 bar; if all three are missing, use that session's daily open. A fill is on time only if it comes from the 09:45
bar. **Every other fill (the 09:50 bar, the 09:55 bar or the daily open) is a substituted fill and counts toward the 2%
budget**; the result reports the three kinds separately. Costs are
unchanged: 5 bp per side plus the 0.3 bp sell fee, which also has to cover the real spread and the limit offset.

**Data.** A separate fetch script downloads only the 09:45 to 10:00 bars (read-only market data) and prints coverage
per symbol (days, first date, fallback share), never any return or performance figure. The file's SHA-256 is committed
in a manifest in `docs/` BEFORE the run, and a run with a different fingerprint is refused. The overlap start date is
whatever the coverage report shows; it is not chosen after seeing results.

**Clarification: round trips.** Everywhere the original and this amendment count round trips (the 30 minimum, the S-B
expectancy), only completed ones count: the exit was executed inside the sample. A position still open at the end of the
sample is not counted. The runner implements this (`count_runs`), with a test.

## 3. Holdout: window and execution path

**Decision (owner, 2026-10-19).** The forward holdout for S-A, S-B and S-C is the one in the original section 8, now with
its end defined, and it is NOT the 252-trading-day locked holdout of the earlier autoresearch protocol. That protocol
is not applied to these three candidates; this is recorded as a choice, not a silent replacement. The shorter window
is weaker evidence and can only falsify, as the original says.

- **Window:** starts 2026-10-08. It ends on the soak completion date as defined in `strategy.md` (section 8), written
  into `docs/holdout.lock` BEFORE any holdout data is read. The end date is never chosen after looking at data.
- **Minimums:** at least 60 trading days, and at least 5 completed round trips (S-A, S-B) or 6 completed windows (S-C).
  Below these, the holdout result is INSUFFICIENT.
- **Use:** single evaluation per candidate, recorded in `docs/holdout.lock` (date, end date, code fingerprint, result);
  a second evaluation is refused by the code.
- **Execution path:** a separate `holdout` command, written and committed before the soak completes, with its
  fingerprint in `docs/holdout.lock` and a tag `holdout-runner-daily-v1`. It uses 09:45 ET fills from the same fetch,
  never the open. The development `run` and `count` commands never read data after 2026-10-06, and `holdout` stays
  refused until those conditions are met.

## 4. Trial accounting

The floor of 31 is a lower bound, not a proof of completeness. Before `RUN_LOCKED` is lifted:

- `count` prints the full inventory (every logged trial, every backfilled family and its note). That output and a
  manual list of every other attempt in the repository history (`zone_reversal`, the `us_momentum` variants,
  `daily_edges_backtest.py`, any LLM hypothesis-lab proposals) are committed as `docs/TRIAL_INVENTORY_daily_v1.md`
  with a total, `N_inventory`.
- The inventory document must contain a line `N_inventory: <integer>`. The runner enforces the formula in code: it reads
  that line, refuses `run` if the file or the line is missing, and computes
  N = max(31, database rows + backfilled + candidates not yet logged, `N_inventory` + candidates not yet counted).
  A small inventory never lowers N.
- Each candidate is counted exactly once, by its name `daily_v1_S-A`, `daily_v1_S-B`, `daily_v1_S-C`. If
  `daily_edges_backtest.py` produced related mean-reversion or turn-of-month trials, they stay counted as separate
  attempts (conservative); S-B and S-C are not merged into them.

## 5. What has to happen before the first real run

1. This amendment merged and tagged `prereg-daily-v1.1`.
2. Intraday fetch run on the server and its manifest (fingerprint) committed.
3. `docs/TRIAL_INVENTORY_daily_v1.md` committed with its `N_inventory:` line. (The code that reads it and enforces the
   formula is already in the runner, with tests; the lock stays until the file exists and the run passes that check.)
4. The runner extended to implement criterion 5 and the 09:45 fill convention (including the substituted-fill count),
   with tests, merged.
5. `RUN_LOCKED` lifted in its own reviewed change.

No result may be produced before all five are done. Nothing here authorizes live orders.
