"""Repo-wide pytest setup.

1. Test-only credentials. Tests must never see real keys: dummy paper keys
   make the executor take its normal (mocked) broker path instead of stub
   mode; live keys are removed outright.
2. Script-style suites (run checks on import, then exit) can't be imported
   by pytest. They are found automatically, skipped here, and run as
   subprocesses by tests/test_script_suites.py instead.
"""
import os
import re
from pathlib import Path

os.environ["APCA_API_KEY_ID"] = "test-dummy-key"
os.environ["APCA_API_SECRET_KEY"] = "test-dummy-secret"
os.environ.pop("APCA_LIVE_API_KEY_ID", None)
os.environ.pop("APCA_LIVE_API_SECRET_KEY", None)
os.environ["LIVE_TRADING"] = "false"
os.environ["US_LIVE"] = "false"

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
