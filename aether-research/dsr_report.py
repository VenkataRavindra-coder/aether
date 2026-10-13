"""Deflated Sharpe + PBO report over the AETHER trial log. Read-only.

    python3 dsr_report.py                    # kurtosis stored RAW (pandas .kurt() + 3)
    python3 dsr_report.py --excess-kurtosis  # only for a script that stores pandas .kurt() as-is
    python3 dsr_report.py --batch B1-2026-10 # PBO for one pre-registered batch

DSR (Bailey & Lopez de Prado): the best Sharpe out of N tries is expected to be
high by luck alone. SR0 is that luck threshold, from N (logged + backfilled trials,
ALL batches) and the spread of Sharpe ratios across logged trials. DSR is the
probability that a strategy's true Sharpe beats SR0, given its sample length,
skew and kurtosis.

PBO (CSCV, pbo.py): per batch, how often the in-sample winner lands in the bottom
half out of sample. Gate: DSR >= 0.95 AND PBO <= 0.05.
"""
import argparse
import json
import math
import os
import sqlite3
import statistics
import sys
from statistics import NormalDist

HERE = os.path.dirname(os.path.abspath(__file__))
N01 = NormalDist()
EULER = 0.5772156649
PERIODS = 252  # metrics are computed on daily returns
DEFAULT_SD_ANNUAL = 0.5  # used only when fewer than 2 trials exist
PBO_MAX = 0.05


def expected_max_sr(n, var):
    if n <= 1 or var <= 0:
        return 0.0
    return math.sqrt(var) * ((1 - EULER) * N01.inv_cdf(1 - 1 / n)
                             + EULER * N01.inv_cdf(1 - 1 / (n * math.e)))


def prob_sr_above(sr, sr0, t, skew, kurt_raw):
    denom = 1 - skew * sr + (kurt_raw - 1) / 4 * sr * sr
    if t < 2 or denom <= 0:
        return float("nan")
    return N01.cdf((sr - sr0) * math.sqrt(t - 1) / math.sqrt(denom))


def _trading_days(start, end):
    import datetime as dt
    a, b = dt.date.fromisoformat(start[:10]), dt.date.fromisoformat(end[:10])
    wd = sum(1 for i in range((b - a).days + 1) if (a + dt.timedelta(i)).weekday() < 5)
    return int(wd * 252 / 261)


def verdict(p):
    if p != p:
        return "n/a"
    return "significant" if p >= 0.95 else "promising" if p >= 0.80 else "could be luck"


def pbo_section(db, only_batch=None):
    """One PBO line per batch. Anything that can't be computed is shown, not hidden."""
    sys.path.insert(0, HERE)
    try:
        import pbo
        import trials
    except ImportError as e:
        print(f"\nPBO unavailable ({e}).")
        return
    con = trials._connect(db)  # also migrates an old schema
    batches = con.execute(
        "SELECT batch, COUNT(*) FROM research_trials WHERE batch IS NOT NULL "
        "AND n_returns > 0 GROUP BY batch ORDER BY batch").fetchall()
    con.close()
    if only_batch is not None:
        batches = [b for b in batches if b[0] == only_batch] or [(only_batch, 0)]
    print("\nPBO by batch (gate: PBO <= 0.05)")
    if not batches:
        print("  no batches with daily returns logged yet")
        return
    for batch, n in batches:
        try:
            _, days, M = pbo.returns_matrix(db, batch=batch)
            if M.size == 0:
                raise ValueError("no trials with daily returns")
            r = pbo.cscv_pbo(M)
        except ValueError as e:
            print(f"  {batch:<20} trials={n:<3} PBO n/a: {e}")
            continue
        gate = "PASS" if r["pbo"] <= PBO_MAX else "FAIL"
        print(f"  {batch:<20} trials={r['n_trials']:<3} days={r['n_days']:<5} "
              f"PBO={r['pbo']:.3f}  mean logit={r['mean_logit']:.2f}  {gate}")


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--excess-kurtosis", action="store_true")
    ap.add_argument("--batch")
    ap.add_argument("--db", default=os.path.join(HERE, "research.sqlite"))
    a = ap.parse_args(argv)
    raw = not a.excess_kurtosis

    con = sqlite3.connect(a.db)
    try:
        rows = con.execute("SELECT id, strategy, symbol, oos_sharpe, oos_json "
                           "FROM research_trials ORDER BY id").fetchall()
    except sqlite3.OperationalError:
        rows = []
    try:
        untracked = con.execute("SELECT family, SUM(count) FROM untracked_trials GROUP BY family").fetchall()
    except sqlite3.OperationalError:
        untracked = []
    con.close()
    if not rows:
        sys.exit("no trials logged yet")

    srs = [r[3] / math.sqrt(PERIODS) for r in rows if r[3] is not None]
    var = statistics.variance(srs) if len(srs) >= 2 else (DEFAULT_SD_ANNUAL / math.sqrt(PERIODS)) ** 2
    n_back = sum(c for _, c in untracked)
    n = len(rows) + n_back
    sr0 = expected_max_sr(n, var)

    print(f"Trials: {len(rows)} logged + {n_back} backfilled = {n} total")
    for fam, c in untracked:
        print(f"  backfilled {fam}: {c}")
    print(f"Spread of trial Sharpes (annual sd): {math.sqrt(var) * math.sqrt(PERIODS):.2f}")
    print(f"Luck threshold SR0 (annual): {sr0 * math.sqrt(PERIODS):.2f}   "
          f"kurtosis read as {'raw' if raw else 'excess'}\n")
    print(f"{'id':>3}  {'strategy':<24} {'OOS SR':>6} {'days':>5}  {'P(SR>0)':>7}  {'DSR':>5}  verdict")
    for rid, strat, sym, oos_sr, oos_json in rows:
        m = json.loads(oos_json or "{}")
        t, approx = m.get("days"), False
        if not t and m.get("start") and m.get("end"):
            t, approx = _trading_days(m["start"], m["end"]), True
        sr = (oos_sr if oos_sr is not None else m.get("sharpe", float("nan"))) / math.sqrt(PERIODS)
        skew = m.get("daily_skew", 0.0) or 0.0
        k = m.get("daily_kurtosis")
        kurt = 3.0 if k is None else (k if raw else k + 3.0)
        psr = prob_sr_above(sr, 0.0, t or 0, skew, kurt)
        dsr = prob_sr_above(sr, sr0, t or 0, skew, kurt)
        days = (f"~{t:>4}" if approx else f"{t:>5}") if t else "  n/a"
        print(f"{rid:>3}  {(strat or '')[:24]:<24} {sr * math.sqrt(PERIODS):>6.2f} {days}  "
              f"{psr:>7.2f}  {dsr:>5.2f}  {verdict(dsr)}")
    print("\nDSR >= 0.95 significant, 0.80-0.95 promising, below 0.80 could be luck.")
    pbo_section(a.db, a.batch)


if __name__ == "__main__":
    main(sys.argv[1:])
