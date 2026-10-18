"""Property tests (Hypothesis) for the order gates: risk_controller and safety_guard.check_live_order.

Build-list item 2 (Oct 9, "verification loop"). The example tests in test_risk_controller.py check
specific cases; these state the invariants and let Hypothesis search for any input that breaks them:

  R1  a closing order with a valid side is always allowed (halt, kill switch, engine, size: nothing traps a position)
  R2  an entry is always blocked while the halt file exists
  R3  crypto never gets a real-money entry
  R4  a non-finite or non-positive notional never passes as an entry
  R5  real-money entries never exceed the per-order cap, and recorded daily spend never exceeds the budget
  R6  preflight() never consumes the daily budget
  R7  if preflight() allows a plan, submitting that plan order by order is allowed for every order
  S1  safety_guard never allows a sell larger than the position (no accidental short)
  S2  safety_guard never allows a sell with no position
  S3  an allowed live entry is never larger than requested and never below the broker minimum

Test-only. Nothing here runs on the server unless pytest is run there. Hypothesis is a dev dependency
(requirements-dev.txt); without it this module is skipped, so the server's suite is unaffected.
"""
import json
import math
from datetime import datetime

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, example, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

import risk_controller as rc  # noqa: E402
import safety_guard as sg  # noqa: E402

SETTINGS = settings(max_examples=300, deadline=None,
                    suppress_health_check=[HealthCheck.function_scoped_fixture])

ENGINES = st.sampled_from(["crypto", "us", "stress", "canary", "unknown", ""])
SIDES = st.sampled_from(["BUY", "SELL", "buy", "sell"])
FINITE_USD = st.floats(min_value=0.01, max_value=10_000, allow_nan=False, allow_infinity=False)
# Most bugs live near the caps ($15/$30 canary, $60/$100 US here), so half the draws land in $0-$150.
NEAR_CAPS = st.one_of(st.floats(min_value=0.01, max_value=150, allow_nan=False), FINITE_USD)
ANY_NOTIONAL = st.one_of(
    st.floats(allow_nan=True, allow_infinity=True),
    st.integers(min_value=-1_000, max_value=1_000_000),
    st.sampled_from([None, "", "abc", "nan", "inf", "-1", "0"]),
)
KILL = st.sampled_from([None, False, True])


@pytest.fixture(autouse=True)
def _gate_env(tmp_path, monkeypatch):
    """Fresh state files and known limits for every test. HALT_FILE is resolved when risk_controller is
    imported, so it is pointed at this test's folder directly."""
    monkeypatch.setenv("RISK_LOG", "0")
    monkeypatch.setenv("AETHER_HOME", str(tmp_path))
    monkeypatch.setenv("AETHER_RISK_STATE", str(tmp_path / "risk.json"))
    monkeypatch.setenv("AETHER_CANARY_RISK_STATE", str(tmp_path / "canary.json"))
    monkeypatch.setenv("RISK_MAX_ORDER_USD_LIVE", "60")
    monkeypatch.setenv("RISK_MAX_DAILY_BUY_USD_LIVE", "100")
    monkeypatch.setenv("CANARY_MAX_ORDER_USD", "15")
    monkeypatch.setenv("CANARY_MAX_DAILY_BUY_USD", "30")
    monkeypatch.setattr(rc, "HALT_FILE", tmp_path / "HALT")
    monkeypatch.setattr(sg, "breaker_latched", lambda: False)
    saved = dict(rc._KILL)
    yield tmp_path
    rc._KILL.clear()
    rc._KILL.update(saved)


def _reset(tmp_path, halted=False, kill=None):
    for f in ("risk.json", "canary.json"):
        (tmp_path / f).unlink(missing_ok=True)
    halt = tmp_path / "HALT"
    halt.touch() if halted else halt.unlink(missing_ok=True)
    for k in rc._KILL:
        rc._KILL[k] = kill


def _spent(tmp_path, engine):
    p = tmp_path / ("canary.json" if engine == "canary" else "risk.json")
    if not p.exists():
        return 0.0
    return float(json.loads(p.read_text()).get("usd", 0.0))


# ---------------------------------------------------------------- risk_controller
@SETTINGS
@given(engine=ENGINES, side=SIDES, notional=ANY_NOTIONAL, real=st.booleans(),
       halted=st.booleans(), kill=KILL)
def test_r1_closing_orders_always_allowed(_gate_env, engine, side, notional, real, halted, kill):
    _reset(_gate_env, halted=halted, kill=kill)
    allowed, reason = rc.check(engine, "SPY", side, notional, True, real)
    assert allowed, reason


@SETTINGS
@given(engine=ENGINES, side=SIDES, notional=ANY_NOTIONAL, real=st.booleans(), kill=KILL)
def test_r2_halt_blocks_every_entry(_gate_env, engine, side, notional, real, kill):
    _reset(_gate_env, halted=True, kill=kill)
    allowed, _ = rc.check(engine, "SPY", side, notional, False, real)
    assert not allowed


@SETTINGS
@given(side=SIDES, notional=FINITE_USD)
def test_r3_crypto_never_live(_gate_env, side, notional):
    _reset(_gate_env, kill=False)
    allowed, _ = rc.check("crypto", "BTC/USD", side, notional, False, True)
    assert not allowed


@SETTINGS
@given(engine=ENGINES, notional=ANY_NOTIONAL, real=st.booleans())
def test_r4_bad_notional_never_enters(_gate_env, engine, notional, real):
    _reset(_gate_env, kill=False)
    try:
        usd = float(notional)
    except (TypeError, ValueError):
        usd = math.nan
    allowed, _ = rc.check(engine, "SPY", "BUY", notional, False, real)
    if not (math.isfinite(usd) and usd > 0):
        assert not allowed


@SETTINGS
@given(engine=st.sampled_from(["us", "canary"]),
       orders=st.lists(NEAR_CAPS, min_size=1, max_size=12))
def test_r5_live_caps_hold_over_any_sequence(_gate_env, engine, orders):
    _reset(_gate_env, kill=False)
    cap, budget = (15.0, 30.0) if engine == "canary" else (60.0, 100.0)
    for usd in orders:
        before = _spent(_gate_env, engine)
        allowed, reason = rc.check(engine, "SPY", "BUY", usd, False, True)
        if allowed:
            assert usd <= cap, reason
        else:
            assert _spent(_gate_env, engine) == before, "a blocked order consumed budget"
        assert _spent(_gate_env, engine) <= budget + 0.01
    # the two budgets never leak into each other
    other = "us" if engine == "canary" else "canary"
    assert _spent(_gate_env, other) == 0.0


PLAN_ORDER = st.tuples(st.sampled_from(["BUY", "SELL"]), NEAR_CAPS, st.booleans(), st.booleans())
# Real-money buys under the per-order cap: the plans whose aggregate can breach the daily budget.
LIVE_BUY = st.tuples(st.just("BUY"), st.floats(min_value=1, max_value=15, allow_nan=False),
                     st.just(False), st.just(True))
PLANS = st.lists(st.one_of(PLAN_ORDER, LIVE_BUY), min_size=1, max_size=10)


@SETTINGS
@given(engine=st.sampled_from(["us", "canary", "stress"]), plan=PLANS)
def test_r6_preflight_never_spends(_gate_env, engine, plan):
    _reset(_gate_env, kill=False)
    rc.preflight(engine, plan)
    assert _spent(_gate_env, "us") == 0.0 and _spent(_gate_env, "canary") == 0.0


@settings(max_examples=600, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(engine=st.sampled_from(["us", "canary", "stress"]), plan=PLANS)
@example(engine="canary", plan=[("BUY", 14.0, False, True), ("BUY", 14.0, False, True), ("BUY", 14.0, False, True)])
@example(engine="us", plan=[("SELL", 100.0, True, True), ("BUY", 50.0, False, True), ("BUY", 55.0, False, True)])
def test_r7_preflight_ok_means_every_order_passes(_gate_env, engine, plan):
    _reset(_gate_env, kill=False)
    ok, _ = rc.preflight(engine, plan)
    if not ok:
        return
    for side, usd, closing, real in plan:
        allowed, reason = rc.check(engine, "SPY", side, usd, closing, real)
        assert allowed, f"preflight allowed the plan but {side} ${usd} was refused: {reason}"


# ---------------------------------------------------------------- safety_guard.check_live_order
NOW = datetime(2026, 10, 15, 11, 0, tzinfo=sg.ET)
QTY = st.floats(min_value=0, max_value=1_000, allow_nan=False, allow_infinity=False)


@pytest.fixture
def live_ledger(_gate_env, monkeypatch):
    """A fresh, healthy live ledger dated NOW, so only the order itself decides."""
    monkeypatch.setattr(sg, "load_ledger", lambda: {
        "last_mark": {"date": NOW.date().isoformat(), "equity": 100.0},
        "prev_close": 100.0, "latched": None, "last_status": {"level": "OK"}})
    return _gate_env


@SETTINGS
@given(held=QTY, sell=QTY, notional=FINITE_USD)
def test_s1_sell_never_exceeds_position(live_ledger, held, sell, notional):
    d = sg.check_live_order("sell", "VTI", notional, held, 100.0, sell_qty=sell, now=NOW)
    if d.allowed:
        assert sell <= held + 1e-9 and held > 0


@SETTINGS
@given(held=st.floats(max_value=0, allow_nan=False, allow_infinity=False), notional=FINITE_USD)
def test_s2_no_sell_without_position(live_ledger, held, notional):
    assert not sg.check_live_order("sell", "VTI", notional, held, 100.0, now=NOW).allowed


@SETTINGS
@given(notional=FINITE_USD, equity=st.floats(min_value=1, max_value=1_000, allow_nan=False))
def test_s3_entry_size_bounds(live_ledger, notional, equity):
    d = sg.check_live_order("buy", "VTI", notional, 0.0, equity, now=NOW)
    if d.allowed:
        assert d.notional <= round(notional, 2) + 1e-9
        assert d.notional >= sg.limits().min_notional - 1e-9


@SETTINGS
@given(notional=FINITE_USD, equity=st.floats(min_value=1, max_value=1_000, allow_nan=False))
def test_s4_halt_blocks_live_entry(live_ledger, notional, equity):
    sg.halt_file().touch()  # the per-test path from conftest's autouse fixture
    assert not sg.check_live_order("buy", "VTI", notional, 0.0, equity, now=NOW).allowed
