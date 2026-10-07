"""Tests for aether-research/research_gate.py: the statistics behind the strategy-truth layer (plan S2, S3,
S8, S10, S11, S13, S19). Synthetic data with fixed seeds; no files, no network.

Run from the repo root:  venv/bin/python3 -m pytest -q tests/test_research_gate.py
"""
import math
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "aether-research"))

import pytest

import dsr_report as dr
import research_gate as rg


def noise(t, n, seed, sd=0.01, mean=0.0):
    return np.random.default_rng(seed).normal(mean, sd, size=(t, n))


# ---------------------------------------------------------------- basic statistics
def test_sharpe_matches_a_hand_calculation():
    r = np.array([0.02, 0.0] * 50)
    expected = r.mean() / r.std(ddof=1) * math.sqrt(252)
    assert rg.sharpe(r) == pytest.approx(expected)
    assert rg.sharpe(np.full(100, 0.001)) == 0.0          # no variance: no Sharpe
    assert rg.sharpe([0.01]) == 0.0


def test_max_drawdown_known_cases():
    assert rg.max_drawdown([0.1, -0.5, 0.2]) == pytest.approx(0.5)
    assert rg.max_drawdown([0.01] * 20) == 0.0
    assert rg.max_drawdown([-0.2]) == pytest.approx(0.2)
    assert rg.max_drawdown([]) == 0.0


@pytest.mark.parametrize("bad", [[0.1, float("nan")], [0.1, float("inf")]])
def test_non_finite_returns_are_rejected(bad):
    with pytest.raises(ValueError):
        rg.sharpe(bad)
    with pytest.raises(ValueError):
        rg.max_drawdown(bad)


def test_moments_use_raw_kurtosis_like_dsr_report():
    skew, kurt = rg.moments(np.random.default_rng(1).normal(size=20000))
    assert abs(skew) < 0.1 and kurt == pytest.approx(3.0, abs=0.2)  # normal data: raw kurtosis near 3


# ---------------------------------------------------------------- deflated Sharpe (S2)
@pytest.mark.parametrize("n", [1, 2, 5, 28, 100, 1000])
@pytest.mark.parametrize("var", [0.0, 1e-6, 1e-4, 0.01])
def test_expected_max_sr_matches_the_existing_implementation(n, var):
    assert rg.expected_max_sr(n, var) == dr.expected_max_sr(n, var)


@pytest.mark.parametrize("sr,sr0,t,skew,kurt", [(0.05, 0.0, 500, 0.0, 3.0), (0.1, 0.03, 2500, -0.5, 6.0),
                                                 (0.0, 0.0, 100, 0.2, 4.0), (0.08, 0.05, 1, 0.0, 3.0)])
def test_prob_sr_above_matches_the_existing_implementation(sr, sr0, t, skew, kurt):
    a, b = rg.prob_sr_above(sr, sr0, t, skew, kurt), dr.prob_sr_above(sr, sr0, t, skew, kurt)
    assert (a == b) or (math.isnan(a) and math.isnan(b))


def test_expected_max_sr_known_values_and_monotonicity():
    assert rg.expected_max_sr(1, 1.0) == 0.0
    assert rg.expected_max_sr(10, 1.0) == pytest.approx(1.57, abs=0.03)      # about 1.54 exact, formula ~1.57
    assert rg.expected_max_sr(100, 1.0) > rg.expected_max_sr(10, 1.0) > rg.expected_max_sr(2, 1.0)
    assert rg.expected_max_sr(28, 4.0) == pytest.approx(2 * rg.expected_max_sr(28, 1.0))


def test_psr_at_zero_is_one_half_and_grows_with_sample_length():
    assert rg.prob_sr_above(0.0, 0.0, 500, 0.0, 3.0) == pytest.approx(0.5)
    assert rg.prob_sr_above(0.05, 0.0, 2000, 0.0, 3.0) > rg.prob_sr_above(0.05, 0.0, 200, 0.0, 3.0)


def test_one_trial_means_dsr_equals_psr_and_many_trials_lower_it():
    r = noise(2500, 1, 3, mean=0.0006)[:, 0]
    one = rg.deflated_sharpe(r, 1, 1e-5)
    assert one["dsr"] == pytest.approx(one["psr"])
    many = rg.deflated_sharpe(r, 100, 1e-5)
    assert many["dsr"] < many["psr"] and many["sr0_annual"] > 0


def test_a_strong_series_passes_and_a_weak_one_does_not():
    strong = noise(2500, 1, 4, mean=0.001)[:, 0]     # annual Sharpe about 1.6 over 10 years
    weak = noise(2500, 1, 5, mean=0.0001)[:, 0]
    var = (0.3 / math.sqrt(252)) ** 2                 # trial Sharpes spread by 0.3 annual
    assert rg.deflated_sharpe(strong, 28, var)["dsr"] > 0.95
    assert rg.deflated_sharpe(weak, 28, var)["dsr"] < 0.80


def test_more_trials_never_help():
    r = noise(2500, 1, 6, mean=0.0005)[:, 0]
    d = [rg.deflated_sharpe(r, n, 1e-5)["dsr"] for n in (1, 10, 100, 1000)]
    assert d == sorted(d, reverse=True)


def test_degenerate_series_give_nan_not_a_pass():
    assert math.isnan(rg.deflated_sharpe(np.full(500, 0.001), 10, 1e-5)["dsr"])
    assert math.isnan(rg.deflated_sharpe([0.01, 0.02], 10, 1e-5)["dsr"])


# ---------------------------------------------------------------- PBO by CSCV (S3)
def test_pbo_of_pure_noise_averages_one_half():
    """One dataset is noisy (sd about 0.19), so the claim is about the average over many datasets."""
    vals = [rg.pbo_cscv(noise(480, 20, 1000 + i), n_blocks=8)["pbo"] for i in range(40)]
    assert 0.40 <= float(np.mean(vals)) <= 0.60
    assert max(vals) - min(vals) > 0.3          # and a single dataset really can land far from 0.5


def naive_pbo(m, n_blocks):
    """Slow, obvious reference: explicit row selection and ranking, no running sums."""
    import itertools
    t, n = m.shape
    size = t // n_blocks
    m = m[t - size * n_blocks:]
    blocks = [np.arange(i * size, (i + 1) * size) for i in range(n_blocks)]
    logits = []
    for c in itertools.combinations(range(n_blocks), n_blocks // 2):
        is_rows = np.concatenate([blocks[i] for i in c])
        oos_rows = np.concatenate([blocks[i] for i in range(n_blocks) if i not in c])
        sr_is = m[is_rows].mean(0) / m[is_rows].std(0, ddof=1)
        sr_oos = m[oos_rows].mean(0) / m[oos_rows].std(0, ddof=1)
        best = int(np.argmax(sr_is))
        rank = 1 + int(np.sum(sr_oos < sr_oos[best]))
        w = rank / (n + 1.0)
        logits.append(math.log(w / (1 - w)))
    logits = np.array(logits)
    return float(np.mean(logits <= 0)), float(logits.mean())


@pytest.mark.parametrize("seed,t,n,blocks", [(30, 240, 6, 6), (31, 245, 9, 6), (32, 400, 4, 8), (33, 300, 12, 4)])
def test_pbo_matches_a_brute_force_reference_exactly(seed, t, n, blocks):
    m = noise(t, n, seed)
    m[:, 0] += 0.003                                      # make the ranking non-trivial
    res = rg.pbo_cscv(m, n_blocks=blocks, max_combos=100000)
    pbo, mean_logit = naive_pbo(m, blocks)
    assert res["pbo"] == pytest.approx(pbo, abs=1e-12) and res["mean_logit"] == pytest.approx(mean_logit, abs=1e-9)


def test_pbo_is_near_zero_when_one_variant_has_a_strong_real_edge():
    m = noise(1600, 40, 8)
    m[:, 17] += 0.002                                    # t-statistic above 5 even on half the data
    assert rg.pbo_cscv(m, n_blocks=16, max_combos=3000, seed=1)["pbo"] < 0.05


def test_a_weaker_edge_gives_a_higher_pbo_than_a_strong_one():
    def pbo_for(edge):
        vals = []
        for i in range(8):
            m = noise(1600, 40, 2000 + i)
            m[:, 17] += edge
            vals.append(rg.pbo_cscv(m, n_blocks=16, max_combos=1000, seed=1)["pbo"])
        return float(np.mean(vals))
    assert pbo_for(0.0005) > pbo_for(0.001) > pbo_for(0.002)


def test_a_variant_with_constant_returns_never_wins_by_default():
    m = noise(800, 6, 40)
    m[:, 3] = 0.0007                                      # constant: zero variance, not an infinite Sharpe
    assert rg.sharpe(m[:, 3]) == 0.0
    res = rg.pbo_cscv(m, n_blocks=8, max_combos=100)
    assert 0.0 <= res["pbo"] <= 1.0


def test_pbo_is_high_when_the_in_sample_winner_is_a_fluke():
    """Each variant earns its profit in a different slice of time: whatever wins in-sample does badly out."""
    t, n = 1600, 16
    m = noise(t, n, 9, sd=0.002)
    block = t // n
    for i in range(n):
        m[i * block:(i + 1) * block, i] += 0.01          # variant i is great only in block i
    assert rg.pbo_cscv(m, n_blocks=16, max_combos=3000, seed=1)["pbo"] > 0.6


def test_pbo_is_reproducible_and_reports_rows_used():
    m = noise(1605, 10, 10)
    a = rg.pbo_cscv(m, n_blocks=8, seed=3)
    b = rg.pbo_cscv(m, n_blocks=8, seed=3)
    assert a == b and a["rows_used"] == (1605 // 8) * 8


def test_pbo_uses_every_split_when_there_are_few():
    res = rg.pbo_cscv(noise(400, 5, 11), n_blocks=6, max_combos=20000)
    assert res["n_splits"] == math.comb(6, 3)


@pytest.mark.parametrize("m,blocks", [(np.zeros((100, 1)), 4), (np.zeros((100, 3)), 3), (np.zeros((100, 3)), 1),
                                      (np.zeros((10, 3)), 16), (np.zeros(100), 4)])
def test_pbo_rejects_bad_input(m, blocks):
    with pytest.raises(ValueError):
        rg.pbo_cscv(m, n_blocks=blocks)


def test_pbo_rejects_nan():
    m = noise(200, 4, 12)
    m[5, 2] = float("nan")
    with pytest.raises(ValueError):
        rg.pbo_cscv(m, n_blocks=4)


# ---------------------------------------------------------------- block bootstrap (S10)
def test_bootstrap_lower_tail_separates_skill_from_noise():
    skilled = rg.block_bootstrap_sharpes(noise(2500, 1, 13, mean=0.001)[:, 0], draws=2000, seed=1)
    flat = rg.block_bootstrap_sharpes(noise(2500, 1, 14, mean=0.0)[:, 0], draws=2000, seed=1)
    assert np.percentile(skilled, 5) > 0 and np.percentile(flat, 5) < 0


def test_bootstrap_is_reproducible_and_has_the_right_shape():
    r = noise(1000, 1, 15)[:, 0]
    a = rg.block_bootstrap_sharpes(r, draws=300, seed=5)
    assert a.shape == (300,) and np.array_equal(a, rg.block_bootstrap_sharpes(r, draws=300, seed=5))
    assert not np.array_equal(a, rg.block_bootstrap_sharpes(r, draws=300, seed=6))


def test_longer_blocks_widen_the_spread_for_autocorrelated_returns():
    rng = np.random.default_rng(16)
    e = rng.normal(0, 0.01, 3000)
    r = np.empty(3000)
    r[0] = e[0]
    for i in range(1, 3000):
        r[i] = 0.4 * r[i - 1] + e[i]                      # positively autocorrelated
    wide = rg.block_bootstrap_sharpes(r, block=42, draws=2000, seed=1).std()
    narrow = rg.block_bootstrap_sharpes(r, block=1, draws=2000, seed=1).std()
    assert wide > narrow * 1.15                           # iid resampling would understate the uncertainty


def test_bootstrap_of_a_constant_series_is_zero_not_astronomical():
    assert np.all(rg.block_bootstrap_sharpes(np.full(300, 0.001), draws=50, seed=1) == 0.0)


def test_bootstrap_rejects_a_series_shorter_than_two_blocks():
    with pytest.raises(ValueError):
        rg.block_bootstrap_sharpes(np.zeros(30), block=21)


# ---------------------------------------------------------------- Monte Carlo drawdown (S11)
def test_mc_drawdown_is_bounded_and_never_below_the_worst_single_loss():
    r = noise(120, 1, 17, sd=0.04, mean=0.005)[:, 0]
    dd = rg.mc_max_drawdown(r, draws=1500, seed=1)
    assert dd.shape == (1500,) and dd.min() >= 0 and dd.max() <= 1
    assert dd.min() >= -r.min() - 1e-12                   # any single loss is a drawdown in every ordering


def test_mc_drawdown_grows_with_volatility_and_is_reproducible():
    r = noise(120, 1, 18, sd=0.03, mean=0.004)[:, 0]
    calm = np.percentile(rg.mc_max_drawdown(r, draws=1500, seed=2), 95)
    wild = np.percentile(rg.mc_max_drawdown(r * 2.5, draws=1500, seed=2), 95)
    assert wild > calm
    assert np.array_equal(rg.mc_max_drawdown(r, draws=200, seed=3), rg.mc_max_drawdown(r, draws=200, seed=3))


def test_mc_drawdown_depends_on_the_ordering_of_the_returns():
    """For [-10%, -10%, +10%, +10%] the drawdown is 19% if the losses come together and 10% if they are split up."""
    dd = rg.mc_max_drawdown([-0.1, -0.1, 0.1, 0.1], draws=600, seed=1)
    assert dd.max() == pytest.approx(0.19, abs=1e-9) and dd.min() == pytest.approx(0.10, abs=1e-9)
    assert len(np.unique(np.round(dd, 6))) >= 2


def test_mc_drawdown_degenerate_cases():
    assert rg.mc_max_drawdown(np.full(20, 0.01), draws=50, seed=1).max() == 0.0
    assert np.allclose(rg.mc_max_drawdown([-0.2, 0.0, 0.0, 0.0], draws=50, seed=1), 0.2)
    with pytest.raises(ValueError):
        rg.mc_max_drawdown([0.1])


# ---------------------------------------------------------------- shuffled-returns null (S8)
def table(n=252, seed=19):
    idx = pd.bdate_range("2020-01-01", periods=n)
    return pd.DataFrame(noise(n, 3, seed), index=idx, columns=["A", "B", "C"])


def test_block_shuffle_keeps_every_row_intact():
    df = table()
    out = rg.block_shuffle_rows(df, block=21, seed=1)
    assert out.index.equals(df.index) and out.shape == df.shape
    assert set(map(tuple, out.round(12).to_numpy())) == set(map(tuple, df.round(12).to_numpy()))
    assert np.allclose(out.corr().to_numpy(), df.corr().to_numpy())     # cross-section preserved
    assert np.allclose(np.sort(out["A"].to_numpy()), np.sort(df["A"].to_numpy()))


def test_block_shuffle_changes_the_order_and_is_reproducible():
    df = table()
    a = rg.block_shuffle_rows(df, block=21, seed=1)
    assert not a.equals(df)
    assert a.equals(rg.block_shuffle_rows(df, block=21, seed=1))
    assert not a.equals(rg.block_shuffle_rows(df, block=21, seed=2))


def test_block_shuffle_leaves_an_incomplete_last_block_in_place():
    df = table(n=260)                                    # 12 full blocks of 21 + 8 left over
    out = rg.block_shuffle_rows(df, block=21, seed=1)
    assert np.array_equal(out.iloc[-8:].to_numpy(), df.iloc[-8:].to_numpy())


def test_block_shuffle_rejects_a_short_table():
    with pytest.raises(ValueError):
        rg.block_shuffle_rows(table(n=30), block=21)


def test_pvalue_vs_null():
    assert rg.pvalue_vs_null(2.0, [0.1] * 99) == pytest.approx(1 / 100)
    assert rg.pvalue_vs_null(0.0, [0.1] * 99) == 1.0
    assert rg.pvalue_vs_null(1.0, [1.0] * 9) == 1.0      # ties count against the real result


# ---------------------------------------------------------------- yearly concentration (S13)
def series_over_years(per_year):
    idx = pd.bdate_range("2018-01-01", periods=252 * len(per_year))
    vals = np.concatenate([np.full(252, v / 252) for v in per_year])
    return pd.Series(vals, index=idx)


def test_one_dominant_year_is_flagged():
    s = series_over_years([0.40, 0.02, 0.02, 0.02])
    assert rg.max_year_share(s) > 0.40


def test_evenly_spread_profit_passes():
    s = series_over_years([0.10, 0.10, 0.10, 0.10, 0.10])
    assert rg.max_year_share(s) == pytest.approx(0.2, abs=0.01)


def test_no_total_profit_gives_nan_not_a_pass():
    assert math.isnan(rg.max_year_share(series_over_years([0.1, -0.2])))


# ---------------------------------------------------------------- profit factor windows (S19)
def test_profit_factor_cases():
    assert rg.profit_factor([0.10, -0.05]) == pytest.approx(2.0)
    assert rg.profit_factor([0.1, 0.2]) == float("inf")
    assert rg.profit_factor([-0.1, -0.2]) == 0.0
    assert math.isnan(rg.profit_factor([0.0, 0.0]))


def test_pf_below_one_is_rare_for_a_real_edge_and_common_without_one():
    rng = np.random.default_rng(20)
    edge = np.where(rng.random(400) < 0.6, 0.05, -0.05)       # 60% winners, equal size
    flat = np.where(rng.random(400) < 0.5, 0.05, -0.05)
    bad = np.where(rng.random(400) < 0.4, 0.05, -0.05)
    f_edge, f_flat, f_bad = (rg.pf_below_one_frequency(x, window=20, draws=4000, seed=1) for x in (edge, flat, bad))
    assert f_edge < 0.30 < f_flat < 0.70 and f_bad > 0.70


def test_pf_windows_edge_cases():
    assert rg.pf_below_one_frequency([0.1] * 30, window=20, draws=500, seed=1) == 0.0
    a = rg.pf_below_one_frequency(np.random.default_rng(21).normal(0, 0.05, 100), draws=300, seed=4)
    assert a == rg.pf_below_one_frequency(np.random.default_rng(21).normal(0, 0.05, 100), draws=300, seed=4)
    with pytest.raises(ValueError):
        rg.pf_below_one_frequency([0.1] * 5, window=20)
