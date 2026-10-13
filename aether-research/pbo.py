"""PBO for a batch of logged trials. Thin wrapper: the calculation is
research_gate.pbo_cscv (plan item S3), the single implementation the gate uses.
This module adds loading from the trial log, a minimum-data policy, and a CLI.

Gate (strategy.md): promotion needs PBO <= 0.05 alongside DSR >= 0.95.

    python pbo.py --batch B1-2026-10
"""
import argparse

import numpy as np

import research_gate as rg
import trials

MIN_BLOCK_DAYS = 20  # policy: fewer days per block gives a noisy, meaningless PBO


def returns_matrix(db=trials.DB, batch=None, strategy_like=None):
    """T x N matrix of daily returns on the days ALL selected trials share."""
    con = trials._connect(db)
    q = ("SELECT r.trial_id, r.day, r.ret FROM trial_returns r "
         "JOIN research_trials t ON t.id = r.trial_id")
    where, args = [], []
    if batch is not None:
        where.append("t.batch = ?"); args.append(batch)
    if strategy_like is not None:
        where.append("t.strategy LIKE ?"); args.append(strategy_like)
    if where:
        q += " WHERE " + " AND ".join(where)
    rows = con.execute(q, args).fetchall()
    con.close()
    if not rows:
        return [], [], np.empty((0, 0))
    by_id = {}
    for tid, day, ret in rows:
        by_id.setdefault(tid, {})[day] = ret
    ids = sorted(by_id)
    days = sorted(set.intersection(*(set(v) for v in by_id.values())))
    M = np.array([[by_id[i][d] for i in ids] for d in days], dtype=float)
    return ids, days, M


def cscv_pbo(M, S=16, min_block_days=MIN_BLOCK_DAYS):
    M = np.asarray(M, dtype=float)
    if M.ndim == 2 and M.shape[0] // max(S, 1) < min_block_days:
        raise ValueError(f"{M.shape[0]} common days; need >= {S * min_block_days} for S={S}")
    r = rg.pbo_cscv(M, n_blocks=S)
    return {"pbo": r["pbo"], "n_trials": r["n_variants"], "n_days": r["rows_used"],
            "S": S, "n_splits": r["n_splits"], "mean_logit": r["mean_logit"]}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch"); ap.add_argument("--like")
    ap.add_argument("--S", type=int, default=16); ap.add_argument("--db", default=trials.DB)
    a = ap.parse_args()
    ids, days, M = returns_matrix(a.db, a.batch, a.like)
    if M.size == 0:
        print("No trials with daily returns match."); raise SystemExit(1)
    r = cscv_pbo(M, a.S)
    print(f"trials={r['n_trials']} days={r['n_days']} ({days[0]}..{days[-1]}) splits={r['n_splits']}")
    print(f"PBO={r['pbo']:.3f}  mean logit={r['mean_logit']:.2f}")
    print("PBO gate:", "PASS" if r["pbo"] <= 0.05 else "FAIL", "(<= 0.05)")
