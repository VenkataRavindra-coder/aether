"""AETHER decision log: an append-only record of what the engine decided and why.

Design rules:
  * never raises into the caller - a logging failure can never block or change a trade
  * one short SQLite write per call (WAL mode), rows are never updated or deleted
  * log_changed() writes only when a slot's signature changes, so 60-second loops don't flood it
"""
import json
import logging
import os
import sqlite3
import subprocess
import threading
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("AETHER_DECISION_DB", os.path.join(_HERE, "aether_decisions.sqlite"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,            -- UTC, ISO seconds
    engine TEXT NOT NULL,        -- crypto | us
    mode TEXT,                   -- SHADOW | PAPER | LIVE ...
    symbol TEXT,
    bar_time TEXT,
    event TEXT NOT NULL,         -- evaluate | entry | exit | blocked | skipped | order | order_failed |
                                 -- kill_switch | error | signals | rebalance | status
    decision TEXT,
    reason TEXT,
    price REAL,
    data_json TEXT,              -- indicators, scores, order details
    code_version TEXT            -- engine git commit
);
CREATE INDEX IF NOT EXISTS ix_decisions_ts ON decisions(ts);
CREATE INDEX IF NOT EXISTS ix_decisions_engine_symbol ON decisions(engine, symbol, ts);
"""

_lock = threading.Lock()
_logger = logging.getLogger("AETHER_DECISIONS")
_conn = None
_version = None
_failures = 0
_last_signature = {}


def _code_version():
    global _version
    if _version is None:
        try:
            # Strip NOTIFY_SOCKET: aether_engine.py runs as a Type=notify systemd service, and a
            # subprocess that inherits that env var triggers a harmless but noisy
            # "Got notification message ... rejected" log line from systemd.
            env = {k: v for k, v in os.environ.items() if k != "NOTIFY_SOCKET"}
            out = subprocess.run(["git", "-C", _HERE, "rev-parse", "--short", "HEAD"],
                                 capture_output=True, text=True, timeout=3, env=env)
            _version = out.stdout.strip() or "unknown"
        except Exception:
            _version = "unknown"
    return _version


def _connect():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, timeout=2, check_same_thread=False, isolation_level=None)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.executescript(_SCHEMA)
    return _conn


def log(engine, event, symbol=None, decision=None, reason=None, price=None, bar_time=None, mode=None, **data):
    """Write one decision row. Returns True on success, False on any failure (never raises)."""
    global _conn, _failures
    try:
        row = (datetime.now(timezone.utc).isoformat(timespec="seconds"), engine, mode, symbol,
               None if bar_time is None else str(bar_time), event,
               None if decision is None else str(decision), None if reason is None else str(reason),
               None if price is None else float(price),
               json.dumps(data, default=str, sort_keys=True) if data else None, _code_version())
        with _lock:
            _connect().execute(
                "INSERT INTO decisions (ts, engine, mode, symbol, bar_time, event, decision, reason, price, "
                "data_json, code_version) VALUES (?,?,?,?,?,?,?,?,?,?,?)", row)
        _failures = 0
        return True
    except Exception as e:
        _failures += 1
        if _failures in (1, 10, 100) or _failures % 1000 == 0:
            _logger.warning(f"decision log write failed ({_failures}x): {type(e).__name__}: {str(e)[:120]}")
        try:
            if _conn is not None:
                _conn.close()
        except Exception:
            pass
        _conn = None
        return False


def log_changed(slot, signature, **kwargs):
    """Log only when `signature` differs from the last one written for `slot`."""
    try:
        if _last_signature.get(slot) == signature:
            return False
        if log(**kwargs):
            _last_signature[slot] = signature
            return True
        return False
    except Exception:
        return False
