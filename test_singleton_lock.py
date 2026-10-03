"""Offline test for the cross-process singleton lock (A4.4). Imports the real aether_engine.py
and redirects its module-level LOCK_FILE to a throwaway temp path -- never the real
~/.aether.lock, which the actual running service holds. Run: python3 test_singleton_lock.py
"""
import os
import tempfile
import importlib.util

tmpdir = tempfile.mkdtemp()
os.environ.update(
    LIVE_TRADING="false", US_LIVE="false", US_EXECUTE="false", US_ENABLED="false",
    STRATEGY_ENABLED="false", TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID="",
    WEBHOOK_SECRET="x", DASHBOARD_PASSWORD="x",
    AETHER_HALT_FILE=os.path.join(tmpdir, "HALT"),
    AETHER_RISK_STATE=os.path.join(tmpdir, "risk.json"),
    AETHER_LOCK_FILE=os.path.join(tmpdir, "lock"),
)

PATH = "/home/azureuser/aether_engine.py"
spec = importlib.util.spec_from_file_location("aether_engine_lock_test", PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

results = []


def t(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL") + ": " + name)


t("LOCK_FILE was correctly redirected to the temp path, not the real ~/.aether.lock",
  m.LOCK_FILE == os.environ["AETHER_LOCK_FILE"])

# First acquisition: must succeed (this is "process 1" starting up).
m._acquire_singleton_lock()
t("the first acquisition succeeds and leaves an open fd", m._lock_fd is not None and not m._lock_fd.closed)
with open(m.LOCK_FILE) as f:
    pid_written = f.read().strip()
t("the lock file records this process's pid", pid_written == str(os.getpid()))

# Second acquisition against the SAME still-open lock: simulates a second AETHER process trying
# to start while the first is still running. open() creates an independent file description
# even within the same process, so flock() genuinely conflicts here exactly as it would across
# two real processes.
first_fd = m._lock_fd
try:
    m._acquire_singleton_lock()
    t("a second acquisition while the first is still held raises SystemExit", False)
except SystemExit:
    t("a second acquisition while the first is still held raises SystemExit", True)

# Release the first lock (simulating process 1 exiting) and confirm a new acquisition then works.
first_fd.close()
m._lock_fd = None
m._acquire_singleton_lock()
t("after the first lock is released, a new acquisition succeeds", m._lock_fd is not None)
m._lock_fd.close()

import shutil
shutil.rmtree(tmpdir, ignore_errors=True)

fails = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(fails)}/{len(results)} passed")
if fails:
    print("FAILED:", fails)
    raise SystemExit(1)
