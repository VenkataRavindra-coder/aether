"""Offline tests for decision_log.py's failure alerting and persistence_degraded() flag (A4.5).
Uses a real throwaway SQLite file, with the write path broken by pointing _connect() at an
unwritable path to simulate a real failure. notify.send is stubbed to capture calls (no network).
Run: python3 test_decision_log.py
"""
import os
import tempfile

tmpdir = tempfile.mkdtemp()
os.environ["AETHER_DECISION_DB"] = os.path.join(tmpdir, "decisions.sqlite")
os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
os.environ["TELEGRAM_CHAT_ID"] = "12345"

import decision_log

results = []


def t(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL") + ": " + name)


sent = []
decision_log.notify.send = lambda text, **kw: sent.append((text, kw)) or True

t("code version was resolved eagerly at import (not lazily)", decision_log._version is not None)
t("persistence_degraded() is False before anything has happened", decision_log.persistence_degraded() is False)

# --- normal write succeeds, no alert ---
ok = decision_log.log("us", "evaluate", symbol="SPY")
t("a normal write succeeds", ok is True)
t("still not degraded after a successful write", decision_log.persistence_degraded() is False)
t("no alert was sent for a successful write", len(sent) == 0)

# --- break the write path: point at a directory that can't hold a usable db file ---
decision_log._conn = None
bad_path = os.path.join(tmpdir, "no_such_dir", "decisions.sqlite")
decision_log.DB_PATH = bad_path

ok = decision_log.log("us", "evaluate", symbol="SPY")
t("a write against an unwritable path fails (returns False, does not raise)", ok is False)
t("persistence_degraded() becomes True after a failure", decision_log.persistence_degraded() is True)
t("failure_count() is 1 after the first failure", decision_log.failure_count() == 1)
t("the FIRST failure sends an alert immediately (not after a threshold)", len(sent) == 1 and "[ALERT]" in sent[0][0])
t("the failure alert uses a cooldown key so it doesn't spam on every call", sent[0][1].get("key") == "decision-log-fail")

sent.clear()
ok = decision_log.log("us", "evaluate", symbol="SPY")
t("a second consecutive failure still returns False", ok is False)
t("failure_count() increments", decision_log.failure_count() == 2)
t("a second failure (not the first) does not send ANOTHER immediate alert", len(sent) == 0)

# --- repair the path and confirm it recovers with its own alert ---
sent.clear()
decision_log._conn = None
good_path = os.path.join(tmpdir, "decisions.sqlite")
decision_log.DB_PATH = good_path
ok = decision_log.log("us", "evaluate", symbol="SPY")
t("a write succeeds again once the path is fixed", ok is True)
t("persistence_degraded() goes back to False on recovery", decision_log.persistence_degraded() is False)
t("failure_count() resets to 0 on recovery", decision_log.failure_count() == 0)
t("recovery sends its own [OK] alert", len(sent) == 1 and "[OK]" in sent[0][0])

import shutil
shutil.rmtree(tmpdir, ignore_errors=True)

fails = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(fails)}/{len(results)} passed")
if fails:
    print("FAILED:", fails)
    raise SystemExit(1)
