"""Repo-wide pytest setup.

1. Test-only credentials. Tests must never see real keys: dummy paper keys
   make the executor take its normal (mocked) broker path instead of stub
   mode; live keys are removed outright.
2. Script-style suites (run checks on import, then sys.exit) can't be
   imported by pytest. They are skipped here and run as subprocesses by
   tests/test_script_suites.py instead.
"""
import os

os.environ["APCA_API_KEY_ID"] = "test-dummy-key"
os.environ["APCA_API_SECRET_KEY"] = "test-dummy-secret"
os.environ.pop("APCA_LIVE_API_KEY_ID", None)
os.environ.pop("APCA_LIVE_API_SECRET_KEY", None)
os.environ["LIVE_TRADING"] = "false"
os.environ["US_LIVE"] = "false"

SCRIPT_TESTS = [
    "test_alpaca_executor.py",
    "test_alpaca_timeout.py",
    "test_cf_access.py",
    "test_decision_log.py",
    "test_halt_allows_exit.py",
    "test_login_lockout.py",
    "test_notify_sync.py",
    "test_singleton_lock.py",
    "test_state_recovery.py",
]
collect_ignore = list(SCRIPT_TESTS)
