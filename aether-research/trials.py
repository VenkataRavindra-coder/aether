"""Shared trial log for AETHER research (aether-research/research.sqlite).

Rule (strategy.md section 8): EVERY backtest variant is logged, winners and losers,
so the deflated Sharpe in dsr_report.py counts the true number of attempts.

    import trials
    trials.log_trial("spy_tlt_tom_v1", "TLT", "daily", params, start, end, split,
                     is_metrics, oos_metrics)

oos_metrics must contain: sharpe (annualised), days, daily_skew, daily_kurtosis
(the same keys the existing metrics() functions already produce).
"""
import datetime
import json
import os
import sqlite3

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "research.sqlite")

_TRIALS = """CREATE TABLE IF NOT EXISTS research_trials (
    id INTEGER PRIMARY KEY, created_at TEXT, strategy TEXT, symbol TEXT, mode TEXT,
    params_json TEXT, data_start TEXT, data_end TEXT, split_date TEXT,
    is_json TEXT, oos_json TEXT, is_sharpe REAL, oos_sharpe REAL,
    oos_trades INTEGER, oos_avg_r REAL)"""
_UNTRACKED = """CREATE TABLE IF NOT EXISTS untracked_trials (
    id INTEGER PRIMARY KEY, created_at TEXT, family TEXT, count INTEGER, note TEXT)"""
REQUIRED_OOS = ("sharpe", "days", "daily_skew", "daily_kurtosis")


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _connect(db):
    con = sqlite3.connect(db)
    con.execute(_TRIALS)
    con.execute(_UNTRACKED)
    return con


def log_trial(strategy, symbol, mode, params, data_start, data_end, split_date,
              is_metrics, oos_metrics, oos_trades=None, oos_avg_r=None, db=DB):
    missing = [k for k in REQUIRED_OOS if k not in (oos_metrics or {})]
    if missing:
        raise ValueError(f"oos_metrics missing {missing}: deflated Sharpe needs them")
    con = _connect(db)
    with con:
        cur = con.execute(
            """INSERT INTO research_trials (created_at, strategy, symbol, mode, params_json,
               data_start, data_end, split_date, is_json, oos_json, is_sharpe, oos_sharpe,
               oos_trades, oos_avg_r) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (_now(), strategy, symbol, mode, json.dumps(params, default=str),
             str(data_start), str(data_end), str(split_date),
             json.dumps(is_metrics, default=float), json.dumps(oos_metrics, default=float),
             float((is_metrics or {}).get("sharpe", "nan")), float(oos_metrics["sharpe"]),
             oos_trades, oos_avg_r))
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
