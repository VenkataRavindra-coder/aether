"""Shared trial log for AETHER research (aether-research/research.sqlite).

Rule (strategy.md section 8): EVERY backtest variant is logged, winners and losers,
so the deflated Sharpe in dsr_report.py counts the true number of attempts.

    import trials
    trials.log_trial("spy_tlt_tom_v1", "TLT", "daily", params, start, end, split,
                     is_metrics, oos_metrics,
                     daily_returns=series, batch="B1-2026-10", data_hash=h)

oos_metrics must contain: sharpe (annualised), days, daily_skew, daily_kurtosis.
daily_returns (needed for PBO) is the strategy's daily return series over
data_start..data_end, NOT the locked holdout: a pandas Series indexed by date,
a {date: return} dict, or a list of (date, return) pairs. The code commit is
recorded automatically.
"""
import datetime
import json
import math
import os
import sqlite3
import subprocess

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "research.sqlite")

_TRIALS = """CREATE TABLE IF NOT EXISTS research_trials (
    id INTEGER PRIMARY KEY, created_at TEXT, strategy TEXT, symbol TEXT, mode TEXT,
    params_json TEXT, data_start TEXT, data_end TEXT, split_date TEXT,
    is_json TEXT, oos_json TEXT, is_sharpe REAL, oos_sharpe REAL,
    oos_trades INTEGER, oos_avg_r REAL)"""
_UNTRACKED = """CREATE TABLE IF NOT EXISTS untracked_trials (
    id INTEGER PRIMARY KEY, created_at TEXT, family TEXT, count INTEGER, note TEXT)"""
_RETURNS = """CREATE TABLE IF NOT EXISTS trial_returns (
    trial_id INTEGER NOT NULL REFERENCES research_trials(id),
    day TEXT NOT NULL, ret REAL NOT NULL,
    PRIMARY KEY (trial_id, day))"""
_NEW_COLS = {"batch": "TEXT", "code_commit": "TEXT", "data_hash": "TEXT",
             "n_returns": "INTEGER"}
REQUIRED_OOS = ("sharpe", "days", "daily_skew", "daily_kurtosis")
MIN_RETURN_DAYS = 60


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _connect(db):
    con = sqlite3.connect(db)
    con.execute(_TRIALS)
    con.execute(_UNTRACKED)
    con.execute(_RETURNS)
    have = {r[1] for r in con.execute("PRAGMA table_info(research_trials)")}
    for col, typ in _NEW_COLS.items():
        if col not in have:  # in-place migration; existing rows keep NULL
            con.execute(f"ALTER TABLE research_trials ADD COLUMN {col} {typ}")
    con.commit()
    return con


def _git_commit():
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        head = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=here,
                              capture_output=True, text=True, timeout=5)
        if head.returncode:
            return "unknown"
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                               cwd=here, capture_output=True, text=True, timeout=5).stdout.strip()
        return head.stdout.strip() + ("-dirty" if dirty else "")
    except Exception:
        return "unknown"


def _normalize_returns(daily_returns):
    """Validate before anything is written: fail closed on bad data."""
    items = daily_returns.items() if hasattr(daily_returns, "items") else daily_returns
    out = {}
    for d, r in items:
        day = str(d)[:10]
        datetime.date.fromisoformat(day)  # raises on a malformed date
        x = float(r)
        if not math.isfinite(x):
            raise ValueError(f"non-finite return on {day}: refusing to log")
        if day in out:
            raise ValueError(f"duplicate day {day} in daily_returns")
        out[day] = x
    if len(out) < MIN_RETURN_DAYS:
        raise ValueError(f"daily_returns has {len(out)} days; need >= {MIN_RETURN_DAYS}")
    return sorted(out.items())


def log_trial(strategy, symbol, mode, params, data_start, data_end, split_date,
              is_metrics, oos_metrics, oos_trades=None, oos_avg_r=None, db=DB,
              *, daily_returns=None, batch=None, data_hash=None):
    missing = [k for k in REQUIRED_OOS if k not in (oos_metrics or {})]
    if missing:
        raise ValueError(f"oos_metrics missing {missing}: deflated Sharpe needs them")
    rets = _normalize_returns(daily_returns) if daily_returns is not None else None
    con = _connect(db)
    with con:  # trial row and its returns commit together or not at all
        cur = con.execute(
            """INSERT INTO research_trials (created_at, strategy, symbol, mode, params_json,
               data_start, data_end, split_date, is_json, oos_json, is_sharpe, oos_sharpe,
               oos_trades, oos_avg_r, batch, code_commit, data_hash, n_returns)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (_now(), strategy, symbol, mode, json.dumps(params, default=str),
             str(data_start), str(data_end), str(split_date),
             json.dumps(is_metrics, default=float), json.dumps(oos_metrics, default=float),
             float((is_metrics or {}).get("sharpe", "nan")), float(oos_metrics["sharpe"]),
             oos_trades, oos_avg_r, batch, _git_commit(), data_hash,
             len(rets) if rets else 0))
        if rets:
            con.executemany("INSERT INTO trial_returns (trial_id, day, ret) VALUES (?,?,?)",
                            [(cur.lastrowid, d, r) for d, r in rets])
    n = con.execute("SELECT COUNT(*) FROM research_trials").fetchone()[0]
    con.close()
    return cur.lastrowid, n


def log_untracked(family, count, note, db=DB):
    """Record past attempts that were never logged, so the trial count stays honest."""
    con = _connect(db)
    with con:
        con.execute("INSERT INTO untracked_trials (created_at, family, count, note) VALUES (?,?,?,?)",
                    (_now(), family, int(count), note))
    con.close()
