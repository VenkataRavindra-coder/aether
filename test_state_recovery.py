"""Offline tests for load_state()/save_state()'s corruption recovery (A4.2/A4.3). Imports the
real aether_engine.py (every execution mode forced off) and redirects its module-level
STATE_FILE to a throwaway temp path -- this must never touch the real
/home/azureuser/aether_state.json. Run: python3 test_state_recovery.py
"""
import os
import json
import shutil
import tempfile
import importlib.util

tmpdir = tempfile.mkdtemp()
os.environ.update(
    LIVE_TRADING="false", US_LIVE="false", US_EXECUTE="false", US_ENABLED="false",
    STRATEGY_ENABLED="false", TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID="",
    WEBHOOK_SECRET="x", DASHBOARD_PASSWORD="x",
    AETHER_HALT_FILE=os.path.join(tmpdir, "HALT"),
    AETHER_RISK_STATE=os.path.join(tmpdir, "risk.json"),
)

PATH = "/home/azureuser/aether_engine.py"
spec = importlib.util.spec_from_file_location("aether_engine_staterecovery_test", PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

# Redirect the module's own STATE_FILE global to our temp path. Every function in the module
# reads this as a global at call time, not a bound parameter, so this safely retargets all of
# load_state/save_state/_quarantine_bad_state_file/_repair_primary_from without touching the
# real state file.
STATE_FILE = os.path.join(tmpdir, "aether_state.json")
m.STATE_FILE = STATE_FILE

results = []


def t(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL") + ": " + name)


def reset():
    for f in os.listdir(tmpdir):
        full = os.path.join(tmpdir, f)
        if f not in ("HALT",) and os.path.isfile(full):
            os.remove(full)
    if m.risk_controller.HALT_FILE.exists():
        m.risk_controller.HALT_FILE.unlink()


# --- normal round trip ---
reset()
s1 = m.default_state()
s1["last_signal"] = "round-trip-marker"
m.save_state(s1)
t("primary file was created", os.path.exists(STATE_FILE))
t(".bak was NOT created yet (no primary existed before this first save)", not os.path.exists(STATE_FILE + ".bak"))
loaded = m.load_state()
t("a normal save/load round-trips the data", loaded["last_signal"] == "round-trip-marker")

# second save: now .bak should appear, holding the PREVIOUS primary content
s2 = m.load_state()
s2["last_signal"] = "second-save-marker"
m.save_state(s2)
t(".bak now exists after a second save", os.path.exists(STATE_FILE + ".bak"))
with open(STATE_FILE + ".bak") as f:
    bak_content = json.load(f)
t(".bak holds the PREVIOUS generation's content, not the latest", bak_content["last_signal"] == "round-trip-marker")

# --- first run: no state file at all -- must NOT trigger safe mode ---
reset()
loaded = m.load_state()
t("first run (no files at all) returns a plain default state", loaded["last_signal"] is None)
t("first run does NOT touch the halt file", not m.risk_controller.HALT_FILE.exists())

# --- corrupt primary, good .bak: recover + repair primary + quarantine the corrupt one ---
reset()
good = m.default_state()
good["last_signal"] = "the-good-backup"
m.save_state(good)  # primary now holds "the-good-backup"
m.save_state(good)  # second save: .bak now also holds "the-good-backup"
with open(STATE_FILE, "w") as f:
    f.write("{not valid json!!")  # corrupt the primary only
loaded = m.load_state()
t("recovers the good content from .bak when the primary is corrupt", loaded["last_signal"] == "the-good-backup")
t("the primary is immediately repaired (valid JSON again) right after recovery",
  json.load(open(STATE_FILE))["last_signal"] == "the-good-backup")
quarantined = [f for f in os.listdir(tmpdir) if ".corrupt." in f]
t("the corrupt primary was quarantined (moved aside), not left in place", len(quarantined) == 1)
t("state corruption recovery (NOT total loss) does not touch the halt file", not m.risk_controller.HALT_FILE.exists())

# the actual A4.2 regression: a save AFTER this recovery must not destroy the backup
m.save_state(loaded)
with open(STATE_FILE + ".bak") as f:
    bak_after_repair = json.load(f)
t("A4.2 fix: a save right after recovery does not corrupt .bak with old bad content",
  bak_after_repair.get("last_signal") == "the-good-backup")

# --- both primary and .bak corrupt: must halt instead of silently wiping state ---
reset()
with open(STATE_FILE, "w") as f:
    f.write("{also not valid")
with open(STATE_FILE + ".bak", "w") as f:
    f.write("{still not valid")
t("halt file does not exist before the corrupted load", not m.risk_controller.HALT_FILE.exists())
loaded = m.load_state()
t("total corruption still returns SOMETHING usable (a default state), not a crash",
  isinstance(loaded, dict) and "positions" in loaded)
t("A4.3 fix: total state corruption creates the halt file instead of silently resetting",
  m.risk_controller.HALT_FILE.exists())
t("...and the halt actually blocks new entries (reusing the real halted() check)",
  m.risk_controller.halted() is True)
quarantined2 = [f for f in os.listdir(tmpdir) if ".corrupt." in f]
t("both corrupt files were quarantined", len(quarantined2) == 2)

reset()
shutil.rmtree(tmpdir, ignore_errors=True)

fails = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(fails)}/{len(results)} passed")
if fails:
    print("FAILED:", fails)
    raise SystemExit(1)
