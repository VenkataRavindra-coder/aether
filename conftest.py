"""Repo-wide pytest setup.

1. Test-only credentials. Tests must never see real keys: dummy paper keys
   make the executor take its normal (mocked) broker path instead of stub
   mode; live keys are set to EMPTY (not removed: alpaca_executor calls
   load_dotenv(), which re-adds any variable that is missing, so on the server
   a removed key comes straight back from the real .env).
3. Never the real halt file. Same reason: popping AETHER_HALT_FILE let
   load_dotenv() restore the server's real path, and tests then created
   /home/ratelark/AETHER_HALT (2026-10-14). Every pytest test now gets its own
   AETHER_HALT_FILE under its tmp_path (autouse fixture below), and the
   process-wide value points into a throwaway directory.
2. Script-style suites (run checks on import, then exit) can't be imported
   by pytest. They are found automatically, skipped here, and run as
   subprocesses by tests/test_script_suites.py instead.
"""
import os
import re
import tempfile
from pathlib import Path

import pytest

os.environ["APCA_API_KEY_ID"] = "test-dummy-key"
os.environ["APCA_API_SECRET_KEY"] = "test-dummy-secret"
os.environ["APCA_LIVE_API_KEY_ID"] = ""
os.environ["APCA_LIVE_API_SECRET_KEY"] = ""
os.environ["LIVE_TRADING"] = "false"
os.environ["US_LIVE"] = "false"
# Never let a test resolve the REAL halt file (on the server .env sets AETHER_HALT_FILE).
TEST_SCRATCH = tempfile.mkdtemp(prefix="aether-tests-")
os.environ["AETHER_HALT_FILE"] = os.path.join(TEST_SCRATCH, "AETHER_HALT")
# Same reason for the ledger folder and the live-buy budget files (.env sets AETHER_HOME).
os.environ["AETHER_HOME"] = TEST_SCRATCH
os.environ["AETHER_RISK_STATE"] = os.path.join(TEST_SCRATCH, ".aether_risk_state.json")
os.environ["AETHER_CANARY_RISK_STATE"] = os.path.join(TEST_SCRATCH, ".aether_canary_risk_state.json")

_ROOT = Path(__file__).resolve().parent
_EXITS = re.compile(r"sys\.exit\(|raise SystemExit")
_SKIP_DIRS = {".venv", ".git", "__pycache__", "node_modules"}


def _find_script_tests():
    found = []
    for p in sorted(_ROOT.rglob("test_*.py")):
        rel = p.relative_to(_ROOT)
        if _SKIP_DIRS & set(rel.parts) or p.name == "test_script_suites.py":
            continue
        text = p.read_text(errors="ignore")
        if _EXITS.search(text) and "pytest.raises(SystemExit" not in text:
            found.append(str(rel))
    return found


SCRIPT_TESTS = _find_script_tests()
collect_ignore = list(SCRIPT_TESTS)


@pytest.fixture(autouse=True)
def _isolated_halt_file(tmp_path, monkeypatch):
    """Per-test halt file. Equals AETHER_HOME/AETHER_HALT for tests that point AETHER_HOME at
    their tmp_path, so safety_guard's and risk_controller's views agree inside a test."""
    monkeypatch.setenv("AETHER_HALT_FILE", str(tmp_path / "AETHER_HALT"))
