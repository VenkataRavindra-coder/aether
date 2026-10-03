"""Deflated Sharpe report over the AETHER trial log. Read-only.

    python3 dsr_report.py                 # assumes daily_kurtosis is EXCESS (pandas .kurt())
    python3 dsr_report.py --raw-kurtosis  # if your metrics store raw kurtosis

Method (Bailey & Lopez de Prado): the best Sharpe out of N tries is expected to be
high by luck alone. SR0 is that luck threshold, from N (logged + backfilled trials)
and the spread of Sharpe ratios across logged trials. DSR is the probability that a
strategy's true Sharpe beats SR0, given its sample length, skew and kurtosis.
"""
import json
import math
import os
import sqlite3
import statistics
import sys
from statistics import NormalDist

N01 = NormalDist()
EULER = 0.5772156649
PERIODS = 252  # metrics are computed on daily returns
DEFAULT_SD_ANNUAL = 0.5  # used only when fewer than 2 trials exist


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


def verdict(p):
    if p != p:
        return "n/a"
    return "significant" if p >= 0.95 else "promising" if p >= 0.80 else "could be luck"


def main(argv):
    raw = "--raw-kurtosis" in argv
    db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "research.sqlite")
    con = sqlite3.connect(db)
    rows = con.execute("SELECT id, strategy, symbol, oos_sharpe, oos_json FROM research_trials ORDER BY id").fetchall()
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
        t = m.get("days")
        sr = (oos_sr if oos_sr is not None else m.get("sharpe", float("nan"))) / math.sqrt(PERIODS)
        skew = m.get("daily_skew", 0.0) or 0.0
        k = m.get("daily_kurtosis")
        kurt = 3.0 if k is None else (k if raw else k + 3.0)
        psr = prob_sr_above(sr, 0.0, t or 0, skew, kurt)
        dsr = prob_sr_above(sr, sr0, t or 0, skew, kurt)
        days = f"{t:>5}" if t else "  n/a"
        print(f"{rid:>3}  {strat[:24]:<24} {sr * math.sqrt(PERIODS):>6.2f} {days}  {psr:>7.2f}  {dsr:>5.2f}  {verdict(dsr)}")
    print("\nDSR >= 0.95 significant, 0.80-0.95 promising, below 0.80 could be luck.")


if __name__ == "__main__":
    main(sys.argv[1:])
