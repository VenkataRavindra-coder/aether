import pytest
import risk_controller as rc

@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(rc, "HALT_FILE", tmp_path / "HALT")
    monkeypatch.setattr(rc, "_KILL", {"crypto": None, "us": None})

def test_unknown_engine_entry_blocked():
    assert rc._decide("usa", "BUY", 50.0, False, False) == (False, "unknown_engine")

def test_unknown_engine_exit_still_allowed():
    assert rc._decide("usa", "SELL", 50.0, True, False) == (True, "closing_always_allowed")

def test_stress_paper_entry_allowed():
    assert rc._decide("stress", "BUY", 50.0, False, False) == (True, "ok")

def test_stress_real_money_blocked():
    assert rc._decide("stress", "BUY", 50.0, False, True) == (False, "live_engine_not_allowed")

def test_crypto_real_money_still_blocked():
    assert rc._decide("crypto", "BUY", 50.0, False, True) == (False, "crypto_live_disabled")

def test_us_entry_blocked_until_kill_evaluated():
    assert rc._decide("us", "BUY", 50.0, False, False) == (False, "kill_switch_not_evaluated")
