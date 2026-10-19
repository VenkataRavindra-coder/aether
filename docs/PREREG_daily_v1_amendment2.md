# Amendment 2 to `docs/PREREG_daily_v1.md`: the trial Sharpe variance

Status: LOCKED at the commit tagged `prereg-daily-v1.2` (created on the merge commit of the pull request that adds this
file). Written 2026-10-19, before any result for S-A, S-B or S-C exists (`run` is still locked). Everything not mentioned
here is unchanged, including Amendment 1.

## 1. What the first real `count` showed

Run on the server on 2026-10-19: the trial database restored after the Azure VM was lost holds **0 logged trials** and a
single backfilled row (family `us_momentum_pre_hetzner`, count 28, note "rows unrecoverable"). N = 28 is therefore a
recorded figure, not something that can be recomputed. The registered variance rule (original section 7: "the variance
of the per-period Sharpe ratios of the logged trials ... including the three new trials once logged") does not work on
that database: with no logged rows it falls back to a default annual SD of 0.5, and once only the three candidates are
logged it would be the variance of three numbers. Neither reproduces the history behind N = 28.

## 2. The rule

The variance `V` of per-period trial Sharpes used in the deflated Sharpe is

> `V = max(V_hist, V_logged)`

- `V_hist = 0.0003242764550264549` per period (annual SD 0.286), the figure stored in the stage-1 evidence
  (`aether-research/us_momentum/results/strategy_truth_yf_20261007_074638.json`, key `trials.var_per_period`, tag
  `evidence-s1-stage1`). The individual Sharpes were not stored, so this is the surviving record of the 28 earlier trials.
- `V_logged` is the variance of the per-period Sharpes of every logged trial, including S-A, S-B and S-C (logged or not yet
  logged), when there are at least two; otherwise it does not apply.

The maximum is deliberate: a larger variance raises the luck threshold and makes the deflated Sharpe harder to pass, so
the new trials can raise the bar but never lower it below the registered history. The 0.5 default no longer applies.
The S2 record in `strategy.md` is unchanged.

## 3. Composition of the 28 and the inventory

The 28 are kept as recorded; their composition (how many were logged, how many estimated, whether the three
`daily_edges_backtest.py` experiments are among them) cannot be recovered. The trial inventory required by Amendment 1
therefore counts anything not provably inside the 28 as additional (conservative), and records what the owner states
about each source of attempts. The runner still refuses `run` without the inventory file.

## 4. Implementation

`daily_candidates.py` implements the rule (`HIST_VAR_PER_PERIOD`, `trial_counts`) with a test that the variance is never
below `V_hist`, that a tight cluster cannot lower it and that a wide spread can raise it.
