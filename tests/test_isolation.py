"""The suite runs on the trading server, next to the real .env. These guard against a test ever
resolving the real halt file or real live keys (2026-10-14: popping AETHER_HALT_FILE let
alpaca_executor's load_dotenv() restore the real path, and tests created the real halt file)."""
import os

import alpaca_executor  # noqa: F401  (its load_dotenv() is the thing being guarded against)
import safety_guard


def test_halt_file_is_per_test(tmp_path):
    assert safety_guard.halt_file() == tmp_path / "AETHER_HALT"


def test_dotenv_cannot_restore_real_halt_or_live_keys(tmp_path):
    from dotenv import load_dotenv
    load_dotenv()                                   # what alpaca_executor does at import
    assert safety_guard.halt_file() == tmp_path / "AETHER_HALT"
    assert os.environ.get("APCA_LIVE_API_KEY_ID", "") == ""
    assert os.environ.get("APCA_LIVE_API_SECRET_KEY", "") == ""


def test_records_shown_on_the_dashboard_are_never_the_real_ones():
    """Tests must not write rows the dashboard presents as real activity (2026-10-21 incident)."""
    import tempfile
    from pathlib import Path

    import decision_log
    from aether_tactical import ledger
    scratch = Path(tempfile.gettempdir()).resolve()
    for p in (Path(decision_log.DB_PATH), ledger.path(), Path(os.environ["AETHER_LOCK_FILE"])):
        p = p.resolve()
        assert scratch in p.parents, p
