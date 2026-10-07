"""AETHER approval-gate tests (plan T2, T3-adjacent, T4, T8, T9, F1 'crash after approval').

Run from the repo root with the service's interpreter:
    venv/bin/python3 -m pytest -q tests/test_approval.py

Fakes only: Telegram, the state file, the decision log and the broker are replaced in memory for each
test. Nothing is sent and nothing is written.
"""
import copy
import datetime
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest

import aether_engine as eng

WEIGHTS = {"DBC": 1 / 3, "EEM": 1 / 3, "QQQ": 1 / 3}
SIG = {"weights": WEIGHTS}
ORDERS = [{"symbol": "SPY", "side": "SELL", "qty": 2.0, "est_value": 1000.0},
          {"symbol": "QQQ", "side": "BUY", "notional": 1000.0, "est_value": 1000.0}]


def et_now(hour, minute=0):
    n = datetime.datetime.now(datetime.timezone.utc).astimezone(eng.NY)
    return n.replace(hour=hour, minute=minute, second=0, microsecond=0)


TODAY = et_now(12).strftime("%Y-%m-%d")
BEFORE_CUTOFF, AFTER_CUTOFF = et_now(11), et_now(15, 35)


class FakeBroker:
    def __init__(self, account="ACC-1", client=object(), balance=None):
        self.client, self._account, self._balance = client, account, balance

    def account_id(self):
        return self._account

    def get_account_balance(self):
        return self._balance


@pytest.fixture(autouse=True)
def env(monkeypatch):
    sent = []
    key = eng._us_state_key()
    state = {key: {"cash": None}, "other": {}}
    monkeypatch.setattr(eng.notify, "send", lambda *a, **k: sent.append(a[0] if a else ""))
    logged = []
    monkeypatch.setattr(eng.decision_log, "log", lambda *a, **k: logged.append(a[1] if len(a) > 1 else ""))
    monkeypatch.setattr(eng.decision_log, "log_changed", lambda *a, **k: None)
    monkeypatch.setattr(eng, "US_STATUS", {})
    monkeypatch.setattr(eng, "US_LIVE", False)
    monkeypatch.setattr(eng, "us_broker", FakeBroker())
    monkeypatch.setattr(eng, "load_state", lambda: state)
    saves = []

    def fake_save(s):
        snap = copy.deepcopy(s)
        saves.append(snap)
        state.clear()
        state.update(snap)

    monkeypatch.setattr(eng, "save_state", fake_save)
    return types.SimpleNamespace(sent=sent, state=state, key=key, saves=saves, mp=monkeypatch, logged=logged)


def proposal(env):
    return env.state[env.key].get("live_proposal")


def gate(orders=ORDERS, weights=WEIGHTS, now=BEFORE_CUTOFF, today=TODAY, equity=5000.0):
    return eng._us_live_gate(copy.deepcopy(orders), {"weights": weights}, "2026-11", today, now, equity)


def approve(env):
    return eng._us_live_decide(f"/approve_{proposal(env)['id']}")


# ---------------------------------------------------------------- proposal lifecycle
def test_no_orders_needs_no_approval(env):
    assert gate(orders=[]) is True
    assert proposal(env) is None and env.sent == []


def test_first_call_creates_a_pending_proposal_and_sends_the_message(env):
    assert gate() is False
    p = proposal(env)
    assert p["status"] == "PENDING" and p["date"] == TODAY
    msg = env.sent[0]
    assert f"/approve_{p['id']}" in msg and f"/reject_{p['id']}" in msg
    assert "Rebalance proposal" in msg and "SELL SPY" in msg and "BUY QQQ $1,000.00" in msg
    assert "Targets:" in msg and "Valid today only" in msg and "[US " in msg


def test_a_pending_proposal_is_not_sent_twice(env):
    gate()
    gate()
    gate()
    assert len(env.sent) == 1


def test_approved_but_before_the_cutoff_waits(env):
    gate()
    approve(env)
    assert gate(now=BEFORE_CUTOFF) is False
    assert "orders go out after 15:30" in eng.US_STATUS["note"]
    assert proposal(env)["status"] == "APPROVED"


def test_approved_after_the_cutoff_executes_exactly_once(env):
    gate()
    approve(env)
    first_id = proposal(env)["id"]
    assert gate(now=AFTER_CUTOFF) is True
    assert proposal(env)["status"] == "USED"
    # a second call (a retry, a restart, a replayed approval) must NOT execute again
    assert gate(now=AFTER_CUTOFF) is False
    assert proposal(env)["status"] == "PENDING" and proposal(env)["id"] != first_id
    assert eng._us_live_decide(f"/approve_{first_id}").startswith("No matching")


def test_the_approval_is_consumed_before_orders_go_out(env):
    """F1 'crash after approval': USED is saved BEFORE the caller submits anything, so a crash at that
    point leaves no usable approval behind (a restart must ask again)."""
    gate()
    approve(env)
    n_saves = len(env.saves)
    assert gate(now=AFTER_CUTOFF) is True
    assert len(env.saves) == n_saves + 1
    assert env.saves[-1][env.key]["live_proposal"]["status"] == "USED"


def test_rejection_blocks_for_the_day(env):
    gate()
    assert "REJECTED" in eng._us_live_decide(f"/reject_{proposal(env)['id']}")
    assert gate(now=AFTER_CUTOFF) is False
    assert "rejected" in eng.US_STATUS["note"]


# ---------------------------------------------------------------- replay, wrong id, expiry
def test_unknown_proposal_id_changes_nothing(env):
    gate()
    before = copy.deepcopy(env.state)
    assert eng._us_live_decide("/approve_deadbeef").startswith("No matching")
    assert env.state == before


def test_second_approve_is_a_no_op(env):
    gate()
    approve(env)
    msg = approve(env)
    assert "already APPROVED" in msg and "nothing changed" in msg


def test_approving_after_a_rejection_does_nothing(env):
    gate()
    pid = proposal(env)["id"]
    eng._us_live_decide(f"/reject_{pid}")
    assert "already REJECTED" in eng._us_live_decide(f"/approve_{pid}")
    assert proposal(env)["status"] == "REJECTED"


def test_a_proposal_from_another_day_expires_when_approved(env):
    gate(today="2000-01-01")
    msg = approve(env)
    assert "expired" in msg and proposal(env)["status"] == "EXPIRED"


def test_a_stale_day_proposal_is_replaced_by_a_new_one(env):
    gate(today="2000-01-01")
    old = proposal(env)["id"]
    assert gate(today=TODAY) is False
    assert proposal(env)["id"] != old and proposal(env)["date"] == TODAY
    assert len(env.sent) == 2


# ---------------------------------------------------------------- the approval is bound to the plan
def test_a_changed_plan_expires_the_old_approval_and_asks_again(env):
    gate()
    approve(env)
    old = proposal(env)["id"]
    other = {"DBC": 0.5, "EEM": 0.5}
    assert gate(weights=other, now=AFTER_CUTOFF) is False
    assert proposal(env)["id"] != old and proposal(env)["status"] == "PENDING"


def test_a_different_set_of_orders_needs_a_new_approval(env):
    gate()
    approve(env)
    more = ORDERS + [{"symbol": "GLD", "side": "BUY", "notional": 100.0, "est_value": 100.0}]
    assert gate(orders=more, now=AFTER_CUTOFF) is False
    assert proposal(env)["status"] == "PENDING"


def test_fewer_orders_than_approved_still_needs_a_fresh_approval(env):
    """The plan key includes the exact order list, which is stricter than _us_within_approval alone."""
    gate()
    approve(env)
    old = proposal(env)["id"]
    assert gate(orders=[ORDERS[1]], now=AFTER_CUTOFF) is False
    assert proposal(env)["id"] != old and proposal(env)["status"] == "PENDING"


def test_replacing_a_proposal_leaves_an_audit_entry(env):
    gate()
    approve(env)
    gate(weights={"DBC": 0.5, "EEM": 0.5}, now=AFTER_CUTOFF)
    assert "proposal_expired" in env.logged


def test_approval_for_one_account_is_not_valid_for_another(env):
    gate()
    approve(env)
    env.mp.setattr(eng, "us_broker", FakeBroker(account="ACC-2"))
    assert gate(now=AFTER_CUTOFF) is False
    assert proposal(env)["status"] == "PENDING"


def test_price_moved_past_the_tolerance_asks_again(env):
    gate()
    approve(env)
    bigger = copy.deepcopy(ORDERS)
    bigger[1]["notional"] = 1100.0  # +10% against the approved $1,000
    old = proposal(env)["id"]
    assert gate(orders=bigger, now=AFTER_CUTOFF) is False
    assert proposal(env)["id"] != old and proposal(env)["status"] == "PENDING"


def test_a_small_price_move_inside_the_tolerance_still_executes(env):
    gate()
    approve(env)
    nudged = copy.deepcopy(ORDERS)
    nudged[1]["notional"] = 1015.0  # +1.5%
    assert gate(orders=nudged, now=AFTER_CUTOFF) is True


# ---------------------------------------------------------------- _us_within_approval
def test_within_approval_rules():
    approved = copy.deepcopy(ORDERS)
    assert eng._us_within_approval(copy.deepcopy(ORDERS), approved) is True
    assert eng._us_within_approval([ORDERS[1]], approved) is True                       # fewer orders are fine
    smaller = [dict(ORDERS[1], notional=500.0)]
    assert eng._us_within_approval(smaller, approved) is True                           # smaller is fine
    assert eng._us_within_approval([dict(ORDERS[1], notional=1021.0)], approved) is False
    assert eng._us_within_approval([dict(ORDERS[0], qty=2.05)], approved) is False       # sell +2.5%
    assert eng._us_within_approval([dict(ORDERS[0], qty=2.03)], approved) is True        # sell +1.5%
    assert eng._us_within_approval([{"symbol": "TLT", "side": "BUY", "notional": 1.0}], approved) is False
    assert eng._us_within_approval([dict(ORDERS[1], side="SELL", qty=1.0)], approved) is False  # wrong side


def test_total_buying_may_not_exceed_the_approved_total():
    approved = [{"symbol": "QQQ", "side": "BUY", "notional": 500.0}, {"symbol": "GLD", "side": "BUY", "notional": 500.0}]
    ok = [{"symbol": "QQQ", "side": "BUY", "notional": 509.0}, {"symbol": "GLD", "side": "BUY", "notional": 509.0}]
    assert eng._us_within_approval(ok, approved) is True            # 1018 <= 1020
    over = [{"symbol": "QQQ", "side": "BUY", "notional": 509.0}, {"symbol": "GLD", "side": "BUY", "notional": 509.0},
            {"symbol": "QQQ", "side": "BUY", "notional": 10.0}]
    assert eng._us_within_approval(over, approved) is False


# ---------------------------------------------------------------- live mode safeguards
def live(env, balance=None, client=object(), account="LIVE-1"):
    env.state.setdefault("us_live", {"cash": None})
    env.key = "us_live"  # live mode keeps its own state slot; proposal(env) must read it
    env.mp.setattr(eng, "US_LIVE", True)
    env.mp.setattr(eng, "US_LIVE_MAX_USD", 100.0)
    env.mp.setattr(eng, "us_broker", FakeBroker(account=account, client=client, balance=balance))


GOOD = {"source": "live", "portfolio_value": "90.00"}


def test_live_without_a_client_is_blocked_and_alerts(env):
    live(env, balance=GOOD, client=None)  # balance is fine: only the missing client can block this
    assert gate(equity=80.0, orders=[ORDERS[1] | {"notional": 50.0}]) is False
    assert proposal(env) is None and "unavailable" in env.sent[0]


def test_live_dollar_cap_blocks_before_any_proposal(env):
    live(env, balance=GOOD)
    big = [dict(ORDERS[1], notional=500.0)]
    assert gate(orders=big, equity=80.0) is False
    assert proposal(env) is None and "dollar cap" in env.sent[0]
    assert gate(orders=[dict(ORDERS[1], notional=50.0)], equity=500.0) is False  # authority above the cap
    assert proposal(env) is None


@pytest.mark.parametrize("bal", [None, {"source": "error"}, {"source": "live", "portfolio_value": "nan"},
                                 {"source": "live", "portfolio_value": "0"}])
def test_live_unusable_account_data_blocks(env, bal):
    live(env, balance=bal)
    assert gate(orders=[dict(ORDERS[1], notional=50.0)], equity=80.0) is False
    assert proposal(env) is None


def test_live_unreadable_account_id_blocks(env):
    live(env, balance=GOOD, account=None)
    assert gate(orders=[dict(ORDERS[1], notional=50.0)], equity=80.0) is False
    assert proposal(env) is None


def test_live_within_caps_creates_a_proposal_labelled_live(env):
    live(env, balance=GOOD)
    assert gate(orders=[dict(ORDERS[1], notional=50.0)], equity=80.0) is False
    assert proposal(env)["status"] == "PENDING"
