"""Independent cross-check of the PBO gate (research_gate.pbo_cscv) using skfolio's splitter.

Why: the PBO <= 0.05 gate is self-written. A bug in it would silently let an overfit strategy through.
This recomputes PBO with a different implementation of the parts most likely to hide a bug:

  * the train/test splits come from skfolio.model_selection.CombinatorialPurgedCV
    (n_folds = S, n_test_folds = S/2, no purge/embargo: exactly the CSCV split set);
  * Sharpe is computed directly from the rows (not from block sums), rank with scipy.

Only the Sharpe floor for flat series (research_gate.SD_FLOOR) is shared, so a flat variant is
treated the same way by both. The gate itself is unchanged; this only reports whether the two agree.

Research-only: skfolio is a dev dependency (requirements-dev.txt), not installed on the server.

    python pbo_crosscheck.py --batch B1-2026-10
Exit codes: 0 agree, 1 disagree, 2 nothing to check.
"""
import argparse
import math

import numpy as np
from scipy.stats import rankdata

import research_gate as rg

TOL = 1e-9


def _sharpe(rows):
    sd = rows.std(axis=0, ddof=1)
    mean = rows.mean(axis=0)
    return np.where(sd > rg.SD_FLOOR, mean / np.where(sd > rg.SD_FLOOR, sd, 1.0), 0.0)


def pbo_skfolio(matrix, n_blocks=16):
    """PBO from skfolio's combinatorial splits. Same inputs and trimming rule as rg.pbo_cscv."""
    from skfolio.model_selection import CombinatorialPurgedCV
    m = np.asarray(matrix, dtype=float)
    t, n = m.shape
    size = t // n_blocks
    m = m[t - size * n_blocks:]                       # same rule: drop the oldest remainder
    cv = CombinatorialPurgedCV(n_folds=n_blocks, n_test_folds=n_blocks // 2)
    logits = []
    for train, tests in cv.split(m):
        oos_idx = np.concatenate([np.asarray(x) for x in tests])
        sr_is, sr_oos = _sharpe(m[train]), _sharpe(m[oos_idx])
        best = int(np.argmax(sr_is))
        rank = rankdata(sr_oos, method="min")[best]   # 1 = worst out of sample, ties share the lowest rank
        w = rank / (n + 1.0)
        logits.append(math.log(w / (1.0 - w)))
    logits = np.array(logits)
    return {"pbo": float(np.mean(logits <= 0)), "mean_logit": float(logits.mean()), "n_splits": len(logits)}


def crosscheck(matrix, n_blocks=16):
    """Both PBOs side by side. Refuses to compare when the gate would sample splits (C(S, S/2) > 20,000),
    because then the two would legitimately differ."""
    if math.comb(n_blocks, n_blocks // 2) > 20000:
        raise ValueError(f"S={n_blocks}: the gate samples splits, so an exact comparison is not possible")
    a = rg.pbo_cscv(matrix, n_blocks=n_blocks)
    b = pbo_skfolio(matrix, n_blocks=n_blocks)
    agree = (a["n_splits"] == b["n_splits"] and abs(a["pbo"] - b["pbo"]) <= TOL
             and abs(a["mean_logit"] - b["mean_logit"]) <= 1e-6)
    return {"gate": a, "skfolio": b, "agree": agree}


if __name__ == "__main__":
    import pbo
    import trials
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch"); ap.add_argument("--like")
    ap.add_argument("--S", type=int, default=16); ap.add_argument("--db", default=trials.DB)
    a = ap.parse_args()
    ids, days, M = pbo.returns_matrix(a.db, a.batch, a.like)
    if M.size == 0:
        print("No trials with daily returns match."); raise SystemExit(2)
    r = crosscheck(M, a.S)
    print(f"trials={M.shape[1]} days={M.shape[0]} S={a.S} splits={r['gate']['n_splits']}")
    print(f"gate     PBO={r['gate']['pbo']:.4f}  mean logit={r['gate']['mean_logit']:.4f}")
    print(f"skfolio  PBO={r['skfolio']['pbo']:.4f}  mean logit={r['skfolio']['mean_logit']:.4f}")
    print("AGREE" if r["agree"] else "DISAGREE: do not trust the PBO gate until this is explained")
    raise SystemExit(0 if r["agree"] else 1)
