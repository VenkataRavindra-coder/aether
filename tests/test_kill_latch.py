"""AETHER kill-switch tests (plan E3): the peak-drawdown kill latches; the daily-loss kill does not.

Run from the repo root with the service's interpreter:
    venv/bin/python3 -m pytest -q tests/test_kill_latch.py

Safe to run anywhere: Telegram, the decision log, the state file and the risk-controller state are
all replaced with in-memory fakes for the duration of each test.
"""
import copy
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest

import aether_engine as eng

TODAY = "2026-10-02"


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    sent, saved = [], []
    key = eng._us_state_key()
    state = {key: {"cash": None}, "other": {}}
    monkeypatch.setattr(eng.notify, "send", lambda *a, **k: sent.append(a[0] if a else ""))
    monkeypatch.setattr(eng.decision_log, "log", lambda *a, **k: None)
    monkeypatch.setattr(eng.risk_controller, "_KILL", {"crypto": None, "us": None})
    monkeypatch.setattr(eng, "US_STATUS", {})
    monkeypatch.setattr(eng, "US_MAX_DRAWDOWN", 0.27)
    monkeypatch.setattr(eng, "US_MAX_DAILY_LOSS", 0.03)
    monkeypatch.setattr(eng, "load_state", lambda: state)

    def fake_save(s):
        snapshot = copy.deepcopy(s)  # s may BE `state`: copy before clearing it
        saved.append(snapshot)
        state.clear()
        state.update(snapshot)

    monkeypatch.setattr(eng, "save_state", fake_save)
    return types_ns(sent=sent, saved=saved, state=state, key=key)


def types_ns(**kw):
    import types
    return types.SimpleNamespace(**kw)


def make_us(equity_now, peak=10000.0, holdings=None, latched=None):
    """One ETF: 10 shares at $100 (value 1000); cash makes up the rest. Yesterday's close equals
    equity_now, so the daily-loss test is silent and only the peak drawdown can trip."""
    h = {"SPY": 10.0} if holdings is None else holdings
    return {"holdings": h, "last_prices": {"SPY": 100.0}, "cash": equity_now - 1000.0,
            "equity_history": [{"d": "2026-09-30", "v": peak}, {"d": "2026-10-01", "v": equity_now}],
            "dd_latched": latched}


def reload_us(env):
    return env.state[env.key]


# ---------------------------------------------------------------- limit boundary
def test_just_below_the_drawdown_limit_is_allowed(isolated):
    assert eng._us_evaluate_kill_switch(make_us(7310.0), TODAY) is False  # -26.9%
    assert isolated.saved == []


def test_just_above_the_drawdown_limit_trips_and_latches(isolated):
    us = make_us(7290.0)  # -27.1%
    assert eng._us_evaluate_kill_switch(us, TODAY) is True
    assert isolated.saved, "drawdown trip must persist a latch"
    assert reload_us(isolated)["dd_latched"]
    assert len(isolated.sent) == 1


# ---------------------------------------------------------------- the latch
def test_latch_survives_equity_recovery(isolated):
    eng._us_evaluate_kill_switch(make_us(7290.0), TODAY)
    persisted = reload_us(isolated)["dd_latched"]
    recovered = make_us(9500.0, latched=persisted)  # only -5% from peak now
    assert eng._us_evaluate_kill_switch(recovered, TODAY) is True


def test_latch_survives_an_engine_restart(isolated, monkeypatch):
    eng._us_evaluate_kill_switch(make_us(7290.0), TODAY)
    persisted = reload_us(isolated)["dd_latched"]
    monkeypatch.setattr(eng, "US_STATUS", {})  # fresh process: kill state unknown
    monkeypatch.setattr(eng.risk_controller, "_KILL", {"crypto": None, "us": None})
    assert eng._us_evaluate_kill_switch(make_us(9900.0, latched=persisted), TODAY) is True
    assert eng.risk_controller._KILL["us"] is True


def test_latch_holds_even_with_no_positions(isolated):
    """Positions closed by hand must not silently re-arm the engine."""
    us = make_us(9000.0, holdings={}, latched="27.10% below peak equity (limit 27%)")
    assert eng._us_evaluate_kill_switch(us, TODAY) is True


def test_manual_clear_after_rebaseline_allows_entries_again(isolated):
    us = make_us(9000.0, peak=9000.0, latched=None)  # latch cleared AND peak re-baselined
    assert eng._us_evaluate_kill_switch(us, TODAY) is False


def test_clearing_the_latch_without_rebaselining_trips_again(isolated):
    us = make_us(7290.0, peak=10000.0, latched=None)
    assert eng._us_evaluate_kill_switch(us, TODAY) is True


def test_persistence_failure_still_blocks_entries(isolated, monkeypatch):
    def boom(_):
        raise OSError("disk full")
    monkeypatch.setattr(eng, "save_state", boom)
    assert eng._us_evaluate_kill_switch(make_us(7290.0), TODAY) is True  # fail closed, no exception


# ---------------------------------------------------------------- daily-loss kill is NOT latched
def test_daily_loss_kill_trips_and_clears_without_a_latch(isolated):
    us = make_us(9600.0, peak=10000.0)
    us["equity_history"] = [{"d": "2026-09-30", "v": 10000.0}, {"d": "2026-10-01", "v": 10000.0}]
    assert eng._us_evaluate_kill_switch(us, TODAY) is True  # -4% today
    assert not reload_us(isolated).get("dd_latched")
    next_day = make_us(9600.0, peak=10000.0)  # yesterday's close is now 9600: no daily loss
    assert eng._us_evaluate_kill_switch(next_day, "2026-10-03") is False


# ---------------------------------------------------------------- existing behaviour kept
def test_unknown_data_never_clears_a_trip(isolated):
    eng._us_evaluate_kill_switch(make_us(7290.0), TODAY)
    unknown = make_us(7290.0)
    unknown["cash"] = None
    assert eng._us_evaluate_kill_switch(unknown, TODAY) is True


def test_first_rebalance_is_not_blocked_when_nothing_is_held(isolated):
    assert eng._us_evaluate_kill_switch({"holdings": {}, "cash": None}, TODAY) is False


# ---------------------------------------------------------------- alert wording
def test_alert_does_not_claim_exits_happen_automatically(isolated):
    eng._us_evaluate_kill_switch(make_us(7290.0), TODAY)
    text = isolated.sent[0].lower()
    assert "exits still work" not in text
    assert "nothing sells automatically" in text
