"""Probability of Backtest Overfitting via CSCV
(Bailey, Borwein, Lopez de Prado, Zhu 2015). Self-written, no third-party package.

Gate (strategy.md): promotion needs PBO <= 0.05 alongside DSR >= 0.95.

    python pbo.py --batch B1-2026-10
"""
import argparse
import itertools
import math

import numpy as np

import trials


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


def _sharpe(s1, s2, n):
    mean = s1 / n
    var = np.maximum(s2 / n - mean ** 2, 0.0) * n / (n - 1)
    sd = np.sqrt(var)
    return np.where(sd > 0, mean / np.where(sd > 0, sd, 1.0) * math.sqrt(252), 0.0)


def cscv_pbo(M, S=16, min_block_days=20):
    M = np.asarray(M, dtype=float)
    if M.ndim != 2:
        raise ValueError("M must be T x N")
    T, N = M.shape
    if N < 2:
        raise ValueError("PBO needs at least 2 trials")
    if S < 4 or S % 2:
        raise ValueError("S must be even and >= 4")
    if not np.isfinite(M).all():
        raise ValueError("non-finite returns in matrix")
    L = T // S
    if L < min_block_days:
        raise ValueError(f"{T} common days; need >= {S * min_block_days} for S={S}")
    blocks = M[T - L * S:].reshape(S, L, N)  # drop the oldest remainder days
    s1, s2 = blocks.sum(axis=1), (blocks ** 2).sum(axis=1)
    half = L * S // 2
    logits, oos_loss = [], 0
    for C in itertools.combinations(range(S), S // 2):
        m = np.zeros(S, dtype=bool); m[list(C)] = True
        is_sr = _sharpe(s1[m].sum(0), s2[m].sum(0), half)
        oos_sr = _sharpe(s1[~m].sum(0), s2[~m].sum(0), half)
        best = int(np.argmax(is_sr))
        v = oos_sr[best]
        rank = (oos_sr < v).sum() + 0.5 * ((oos_sr == v).sum() - 1) + 1
        w = rank / (N + 1)
        logits.append(math.log(w / (1 - w)))
        oos_loss += v < 0
    logits = np.array(logits)
    return {"pbo": float((logits <= 0).mean()),  # ties count as overfit (conservative)
            "n_trials": N, "n_days": L * S, "S": S, "n_splits": len(logits),
            "median_logit": float(np.median(logits)),
            "prob_oos_loss": oos_loss / len(logits)}


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
    print(f"PBO={r['pbo']:.3f}  median logit={r['median_logit']:.2f}  P(OOS loss)={r['prob_oos_loss']:.3f}")
    print("PBO gate:", "PASS" if r["pbo"] <= 0.05 else "FAIL", "(<= 0.05)")
