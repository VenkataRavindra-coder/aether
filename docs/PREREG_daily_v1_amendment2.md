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
  `evidence-s1-stage1`).
- `V_logged` is the variance of the per-period Sharpes of every logged trial, including S-A, S-B and S-C (logged or not yet
  logged), when there are at least two; otherwise it does not apply.
- The 0.5-annual-SD default of `dsr_report.py` no longer applies to the gating figure.

### Provenance (verified, with a test)
The stored variance is the one the S2 result used. With `V_hist` and N = 28, `expected_max_sr` reproduces the recorded
luck threshold (0.5845 annualised) and the recorded deflated Sharpe (0.802, recomputed 0.802 from the recorded Sharpe,
skew, kurtosis and 4,931 days). With the old 0.5 default the same inputs give a DSR of about 0.14. The test
`test_stored_variance_reproduces_the_recorded_s2_threshold` checks this against the committed file. The same file shows
`n_total` 28 = `n_logged` 4 + `n_backfilled` 24, written by the same run (data SHA `e9524a2a`, code SHA `c879622e`).
By the construction of `trial_stats`, `V_hist` is the variance of the Sharpes of the **4 logged** trials only; the 24
backfilled attempts carried no Sharpes.

### Limitations, stated plainly
- `max(V_hist, V_logged)` is a registered approximation. It is **not** a reconstruction of the combined variance of all 28
  attempts, which cannot be recovered.
- It is **not uniformly more conservative** than the previous behavior. It lowers the no-history default from 0.5 to
  0.286 annual SD, which raises the deflated Sharpe the candidates would get (for S2's own numbers, 0.14 becomes 0.80). It
  was chosen because it is the measured spread of real trials and holds the candidates to the same standard S2 was held
  to; the 0.5 figure was an arbitrary placeholder for "fewer than two trials".
- The deflated Sharpe is conditional on N being complete, and the composition of the 28 and the other sources of attempts
  are partly unknown (section 3).

### Reported sensitivities (never gating)
Every result shows the criterion-1 DSR and next to it the DSR recomputed (a) with the old 0.5 default variance and (b) in a
**stress scenario with N doubled**. Doubling N is a stress test of how fragile the result is, not a guarantee that N is
complete: unknown attempts could exceed it. For each sensitivity the runner computes what criterion 1 and the verdict
would be if that DSR were used, and marks the candidate **SENSITIVE** if criterion 1 or the verdict would change
(`sensitivity_flags`, `verdict_sensitive` in the result and a SENSITIVE line in the printed report). A candidate that
passes only under the registered variance and N is therefore visibly fragile. The gating figure is unchanged.

## 3. Composition of the 28 and the inventory

The 28 are kept as recorded; their composition (how many were logged, how many estimated, whether the three
`daily_edges_backtest.py` experiments are among them) cannot be recovered. Assumptions for the inventory required by
Amendment 1, to be stated in `docs/TRIAL_INVENTORY_daily_v1.md`:

- `daily_edges_backtest.py`: unknown whether it ever ran. **Documented assumption: its 3 experiments (MR on SPY, MR on
  QQQ, TOM on SPY) count as additional**, giving `N_inventory` of at least 31 (28 + 3) and N of at least 34.
- `zone_reversal` (crypto): count and logging status unknown. Recorded as UNKNOWN and unquantified, not as zero.
- Nightly LLM hypothesis lab: no code exists in the repository. That is weak evidence it never ran, not proof. Recorded
  as UNKNOWN.
- Unknown items are listed, never silently counted as zero. The N-doubled stress scenario above shows how fragile a
  result is to an understated N; it does not bound it.

The runner still refuses `run` without the inventory file.

## 4. Implementation

`daily_candidates.py` implements the rule (`HIST_VAR_PER_PERIOD`, `trial_counts`) and the two reported sensitivities, with
tests: the variance is never below `V_hist`, a tight cluster cannot lower it, a wide spread can raise it, the stored
variance reproduces the recorded S2 threshold, the sensitivities are shown as "reported only", and the flags mark a pass
that a sensitivity would turn into a fail (and the reverse) and keep an INSUFFICIENT criterion INSUFFICIENT.
