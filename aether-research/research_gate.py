"""Statistics for AETHER's strategy-truth layer (plan S2, S3, S7-S11, S13, S19). Pure functions, no file or
network access, deterministic given a seed. Research only: nothing here is imported by the trading engine.

Conventions: `r` is a 1-D array of PERIOD returns (daily unless stated). Sharpe ratios are annualised unless the
name ends in `_per_period`. Drawdowns are positive fractions (0.2 = a 20% fall from a peak).
"""
from __future__ import annotations

import itertools
import math
from statistics import NormalDist

import numpy as np
import pandas as pd

PERIODS = 252
SD_FLOOR = 1e-12  # a standard deviation below this is floating-point dust, not variability
N01 = NormalDist()
EULER = 0.5772156649


# ------------------------------------------------------------------ basic statistics
def _arr(r):
    a = np.asarray(r, dtype=float).ravel()
    if a.size and not np.all(np.isfinite(a)):
        raise ValueError("returns contain NaN or inf")
    return a


def sharpe(r, periods=PERIODS):
    a = _arr(r)
    if a.size < 2:
        return 0.0
    sd = a.std(ddof=1)
    return float(a.mean() / sd * math.sqrt(periods)) if sd > SD_FLOOR else 0.0


def max_drawdown(r):
    a = _arr(r)
    if a.size == 0:
        return 0.0
    eq = np.cumprod(1.0 + a)
    peak = np.maximum.accumulate(np.concatenate(([1.0], eq)))[1:]
    return float(np.max(1.0 - eq / peak))


def moments(r):
    """(skew, RAW kurtosis) as the DSR formula wants them (pandas .kurt() + 3, same as dsr_report)."""
    s = pd.Series(_arr(r))
    return float(s.skew()), float(s.kurt() + 3.0)


# ------------------------------------------------------------------ deflated Sharpe (S2)
def expected_max_sr(n, var):
    """Expected best per-period Sharpe among n unskilled trials whose Sharpe variance is `var`."""
    if n <= 1 or var <= 0:
        return 0.0
    return math.sqrt(var) * ((1 - EULER) * N01.inv_cdf(1 - 1 / n) + EULER * N01.inv_cdf(1 - 1 / (n * math.e)))


def prob_sr_above(sr, sr0, t, skew, kurt_raw):
    denom = 1 - skew * sr + (kurt_raw - 1) / 4 * sr * sr
    if t < 2 or denom <= 0:
        return float("nan")
    return N01.cdf((sr - sr0) * math.sqrt(t - 1) / math.sqrt(denom))


def deflated_sharpe(r, n_trials, trial_sr_var_per_period, periods=PERIODS):
    """DSR of the return series `r` after `n_trials` attempts (logged + backfilled + every variant).
    `trial_sr_var_per_period` is the variance of the per-period Sharpe ratios across the trials."""
    a = _arr(r)
    if a.size < 3 or a.std(ddof=1) <= SD_FLOOR:
        return {"dsr": float("nan"), "psr": float("nan"), "sr_annual": 0.0, "sr0_annual": 0.0, "t": int(a.size)}
    sr = float(a.mean() / a.std(ddof=1))
    skew, kurt = moments(a)
    sr0 = expected_max_sr(n_trials, trial_sr_var_per_period)
    return {"dsr": prob_sr_above(sr, sr0, a.size, skew, kurt), "psr": prob_sr_above(sr, 0.0, a.size, skew, kurt),
            "sr_annual": sr * math.sqrt(periods), "sr0_annual": sr0 * math.sqrt(periods), "t": int(a.size)}


# ------------------------------------------------------------------ PBO by CSCV (S3)
def pbo_cscv(matrix, n_blocks=16, max_combos=20000, seed=0):
    """Probability of Backtest Overfitting (Bailey, Borwein, Lopez de Prado, Zhu).

    matrix: T x N array, one column of returns per tried variant. The T rows are cut into `n_blocks`
    contiguous blocks; every way of choosing half of them as in-sample is tried (a random sample of
    `max_combos` if there are more). The in-sample winner's out-of-sample rank gives a logit; PBO is the share
    of splits where the winner is at or below the out-of-sample median (logit <= 0)."""
    m = np.asarray(matrix, dtype=float)
    if m.ndim != 2 or m.shape[1] < 2:
        raise ValueError("matrix must be T x N with N >= 2 variants")
    if not np.all(np.isfinite(m)):
        raise ValueError("matrix contains NaN or inf")
    if n_blocks < 2 or n_blocks % 2:
        raise ValueError("n_blocks must be an even number >= 2")
    t, n = m.shape
    size = t // n_blocks
    if size < 2:
        raise ValueError("too few rows for this many blocks")
    m = m[t - size * n_blocks:]                                  # drop the oldest remainder
    blocks = m.reshape(n_blocks, size, n)
    s1, s2 = blocks.sum(axis=1), (blocks ** 2).sum(axis=1)       # per block, per variant
    half = n_blocks // 2
    combos = list(itertools.combinations(range(n_blocks), half))
    if len(combos) > max_combos:
        rng = np.random.default_rng(seed)
        combos = [combos[i] for i in rng.choice(len(combos), size=max_combos, replace=False)]
    all_idx = np.arange(n_blocks)
    n_is = half * size

    def sr_from_sums(a1, a2, cnt):
        mean = a1 / cnt
        var = (a2 - cnt * mean ** 2) / (cnt - 1)
        sd = np.sqrt(np.maximum(var, 0.0))
        return np.where(sd > SD_FLOOR, mean / np.where(sd > SD_FLOOR, sd, 1.0), 0.0)

    logits = np.empty(len(combos))
    for k, c in enumerate(combos):
        c = np.array(c)
        rest = np.setdiff1d(all_idx, c)
        sr_is = sr_from_sums(s1[c].sum(0), s2[c].sum(0), n_is)
        sr_oos = sr_from_sums(s1[rest].sum(0), s2[rest].sum(0), n_is)
        best = int(np.argmax(sr_is))
        rank = 1 + int(np.sum(sr_oos < sr_oos[best]))             # 1 = worst, n = best out of sample
        w = rank / (n + 1.0)
        logits[k] = math.log(w / (1.0 - w))
    return {"pbo": float(np.mean(logits <= 0)), "mean_logit": float(logits.mean()), "n_splits": len(combos),
            "n_variants": n, "rows_used": size * n_blocks}


# ------------------------------------------------------------------ resampling (S8, S10, S11)
def block_bootstrap_sharpes(r, block=21, draws=5000, seed=0, periods=PERIODS, chunk=250):
    """Annualised Sharpe of `draws` moving-block-bootstrap resamples of `r` (S10).

    The block start positions for ALL draws are generated first, so the result depends only on the seed, never on
    `chunk`. The resamples are then built `chunk` at a time, which keeps memory small (about 35 MB for 5,000 draws
    over 5,000 days instead of several hundred MB): this runs on the machine that also runs the trading engine."""
    a = _arr(r)
    if a.size < 2 * block:
        raise ValueError("series too short for this block length")
    rng = np.random.default_rng(seed)
    n_blocks = math.ceil(a.size / block)
    starts = rng.integers(0, a.size - block + 1, size=(draws, n_blocks))
    offs = np.arange(block)
    out = np.empty(draws)
    for lo in range(0, draws, chunk):
        st = starts[lo:lo + chunk]
        idx = (st[:, :, None] + offs[None, None, :]).reshape(len(st), -1)[:, :a.size]
        s = a[idx]
        sd = s.std(axis=1, ddof=1)
        out[lo:lo + chunk] = np.where(sd > SD_FLOOR, s.mean(axis=1) / np.where(sd > SD_FLOOR, sd, 1.0), 0.0)
    return out * math.sqrt(periods)


def mc_max_drawdown(period_returns, draws=10000, seed=0):
    """Max drawdown of `draws` random re-orderings of the same returns (S11). Terminal wealth never changes."""
    a = _arr(period_returns)
    if a.size < 2:
        raise ValueError("need at least 2 returns")
    rng = np.random.default_rng(seed)
    out = np.empty(draws)
    for i in range(draws):
        out[i] = max_drawdown(rng.permutation(a))
    return out


def block_shuffle_rows(df, block=21, seed=0):
    """Shuffle whole ROWS of a returns table in blocks (S8). Every row stays intact, so the cross-section
    (correlations between assets on a day) is preserved; time structure beyond `block` days is destroyed."""
    n = len(df)
    if n < 2 * block:
        raise ValueError("table too short for this block length")
    rng = np.random.default_rng(seed)
    starts = list(range(0, n - n % block, block))
    order = rng.permutation(len(starts))
    rows = np.concatenate([np.arange(starts[i], starts[i] + block) for i in order])
    tail = np.arange(n - n % block, n)                           # keep the incomplete last block in place
    out = df.iloc[np.concatenate([rows, tail])].copy()
    out.index = df.index                                         # same dates, shuffled contents
    return out


def pvalue_vs_null(real, null_values):
    """Share of null results at least as good as the real one (one-sided, with the +1 correction)."""
    null = np.asarray(null_values, dtype=float)
    return float((1 + np.sum(null >= real)) / (1 + null.size))


# ------------------------------------------------------------------ concentration (S13) and PF windows (S19)
def yearly_profit_shares(returns):
    """returns: pd.Series of period returns with a DatetimeIndex. Share of total profit earned in each calendar
    year (profit = sum of returns). Meaningful only when the total is positive; otherwise NaN."""
    s = returns.astype(float)
    per_year = s.groupby(s.index.year).sum()
    total = per_year.sum()
    return per_year / total if total > 0 else per_year * float("nan")


def max_year_share(returns):
    sh = yearly_profit_shares(returns)
    return float(sh.max()) if len(sh) and not sh.isna().all() else float("nan")


def profit_factor(trade_returns):
    a = _arr(trade_returns)
    gains, losses = a[a > 0].sum(), -a[a < 0].sum()
    if losses == 0:
        return float("inf") if gains > 0 else float("nan")
    return float(gains / losses)


def pf_below_one_frequency(trade_returns, window=20, draws=5000, seed=0):
    """How often does a window of `window` closed trades show profit factor < 1 by chance alone, if trades are
    drawn at random (with replacement) from the backtest's own trade returns? (S19)"""
    a = _arr(trade_returns)
    if a.size < window:
        raise ValueError("fewer trades than the window")
    rng = np.random.default_rng(seed)
    d = rng.choice(a, size=(draws, window), replace=True)
    gains = np.where(d > 0, d, 0.0).sum(axis=1)
    losses = -np.where(d < 0, d, 0.0).sum(axis=1)
    pf_lt_1 = np.where(losses > 0, gains < losses, False)
    return float(pf_lt_1.mean())
