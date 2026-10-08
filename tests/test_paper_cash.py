"""AETHER paper-mode cash and order-id tests (plan E5 and E8).

PAPER mode keeps a SLEEVE of a much larger paper account and tracks the sleeve's cash in its own state file, saved
only at the end of a rotation. A crash between the sells and the buys therefore loses the proceeds. These tests
kill the process (a BaseException nobody catches, like kill -9) at the worst moments and check the next run.

They also model Alpaca refusing a repeated client_order_id: the real executor then hands back the OLD order.

Run from the repo root:  venv/bin/python3 -m pytest -q tests/test_paper_cash.py
Fakes only: the broker, Telegram, the decision log and the state file are in memory.
"""
import copy
import datetime
import pathlib
import sys
import time
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest

import aether_engine as eng
import us_strategy as us

UNIVERSE = us.UNIVERSE
WEIGHTS = {"DBC": 1 / 3, "EEM": 1 / 3, "QQQ": 1 / 3}
MKEY = "2026-11"
NOW_ET = datetime.datetime(2026, 11, 2, 15, 35, tzinfo=eng.NY)
PRICES = {s: 100.0 for s in UNIVERSE}
SLEEVE = 1700.0                      # 700 of sleeve cash + 10 SPY at 100


class Crash(BaseException):
    """The process dies here: BaseException, so no `except Exception` in the engine can catch it."""


class PaperBroker:
    paper = True
    client = object()

    def __init__(self, positions=None, cash=99000.0, script=None, crash_on=None):
        self.positions = dict(positions or {})
        self.cash = cash
        self.script = script or {}
        self.crash_on = crash_on
        self.consecutive_errors = 0
        self.submitted, self.coids, self._orders, self._by_coid = [], [], {}, {}
        self.balance_reads = 0
        self.balance_override = None

    def submit_market_order(self, symbol, side, qty=None, notional=None, client_order_id=None, strategy="us"):
        if (symbol, side) == self.crash_on:
            raise Crash()
        if client_order_id and client_order_id in self._by_coid:
            # Alpaca refuses a repeated id; the executor then returns the order that already exists
            return {"status": "submitted", "order_id": self._by_coid[client_order_id], "recovered": True}
        oid = f"{symbol}-{side}-{len(self.submitted)}"
        self.submitted.append((symbol, side))
        self.coids.append(client_order_id)
        self._orders[oid] = (symbol, side, qty, notional, self.script.get((symbol, side), "filled"))
        if client_order_id:
            self._by_coid[client_order_id] = oid
        return {"status": "submitted", "order_id": oid}

    def wait_for_fill(self, order_id, timeout=90.0, poll=2.0):
        symbol, side, qty, notional, mode = self._orders[order_id]
        if mode == "filled_done":                       # already filled earlier: report it again
            mode = "filled"
        full = qty if side == "SELL" else notional / 100.0
        if mode == "filled":
            self.cash += (-full if side == "BUY" else full) * 100.0
            self.positions[symbol] = self.positions.get(symbol, 0.0) + (full if side == "BUY" else -full)
            if self.positions[symbol] <= 1e-9:
                del self.positions[symbol]
            self._orders[order_id] = (symbol, side, qty, notional, "filled_done")
            return {"state": "filled", "filled_qty": full, "avg_price": 100.0}
        if mode in ("canceled", "rejected", "expired"):
            return {"state": mode, "filled_qty": 0.0, "avg_price": None}
        return {"state": "new", "filled_qty": 0.0, "avg_price": None}

    def cancel_order(self, order_id):
        return True

    def get_positions_map(self):
        return dict(self.positions)

    def open_order_symbols(self):
        return set()

    def account_id(self):
        return "ACC-1"

    def get_account_balance(self):
        self.balance_reads += 1
        if self.balance_override is not None:
            return self.balance_override
        return {"source": "live", "cash": str(self.cash), "portfolio_value": str(self.cash)}


def make_sig():
    return {"weights": dict(WEIGHTS), "scores": {s: 0.1 for s in us.RISK_ETFS}, "selected": ["DBC", "EEM", "QQQ"],
            "signal_date": "2026-10-30", "latest_close": {s: 100.0 for s in UNIVERSE}}


@pytest.fixture
def env(monkeypatch):
    sent, logged, saves = [], [], []
    key = eng._us_state_key()
    state = {key: eng.default_us_state(), "other": {}}
    broker = PaperBroker(positions={"SPY": 10.0}, cash=99000.0)
    sig = make_sig()
    monkeypatch.setattr(eng.notify, "send", lambda *a, **k: sent.append({"text": a[0] if a else "", **k}))
    monkeypatch.setattr(eng.decision_log, "log", lambda *a, **k: logged.append((a, k)))
    monkeypatch.setattr(eng.decision_log, "log_changed", lambda *a, **k: None)
    monkeypatch.setattr(eng.risk_controller, "_KILL", {"crypto": None, "us": None})
    monkeypatch.setattr(eng.risk_controller, "halted", lambda: False)
    monkeypatch.setattr(eng.risk_controller, "preflight", lambda engine, orders: (True, "ok"))
    monkeypatch.setattr(eng, "US_STATUS", {"retry_after": 0.0, "preview_at": time.time(), "note": None, "last_error": None})
    monkeypatch.setattr(eng, "US_EXECUTE", True)
    monkeypatch.setattr(eng, "US_LIVE", False)
    monkeypatch.setattr(eng, "US_ALLOCATION_USD", 5000.0)
    monkeypatch.setattr(eng, "US_RETRY_SECONDS", 900)
    monkeypatch.setattr(eng, "BROKER_ERROR_FREEZE_THRESHOLD", 3)
    monkeypatch.setattr(eng, "US_MAX_PRICE_DEV", 0.10)
    monkeypatch.setattr(eng, "_us_gate_active", lambda: False)
    monkeypatch.setattr(eng, "uae_str", lambda: "UAE")
    monkeypatch.setattr(eng, "us_broker", broker)
    monkeypatch.setattr(eng, "_us_holdings", lambda us_: {s: q for s, q in broker.get_positions_map().items()
                                                          if s in UNIVERSE and q > 1e-9})
    monkeypatch.setattr(eng, "us_signals", lambda fetch, today: copy.deepcopy(sig))
    monkeypatch.setattr(eng, "load_state", lambda: copy.deepcopy(state))   # like the real one: a fresh read each time

    def fake_save(s):
        snap = copy.deepcopy(s)
        saves.append(snap)
        state.clear()
        state.update(snap)

    monkeypatch.setattr(eng, "save_state", fake_save)
    return types.SimpleNamespace(sent=sent, logged=logged, saves=saves, state=state, key=key, broker=broker, mp=monkeypatch)


def u(env):
    return env.state[env.key]


def seed_sleeve(env, cash=700.0, baseline=99000.0):
    """The sleeve holds `cash` in cash plus 10 SPY (value 1,000); the account cash then was `baseline`."""
    u(env).update(cash=cash, holdings={"SPY": 10.0}, last_rebalance_month="2026-10", last_prices={"SPY": 100.0})
    if baseline is not None:
        u(env)["broker_cash_at_save"] = baseline


def cycle(env, now=NOW_ET, is_open=True):
    eng._us_cycle_inner(lambda syms: {}, lambda symbols: {s: PRICES[s] for s in symbols if s in PRICES},
                        lambda: (is_open, now))


def sleeve_value(env):
    """What the sleeve is really worth: stored cash plus the broker's holdings at 100."""
    return u(env)["cash"] + sum(env.broker.positions.get(s, 0.0) * 100.0 for s in UNIVERSE)


def keys(env):
    return [s.get("key") for s in env.sent]


# ---------------------------------------------------------------- E5: the process dies mid-rotation
def test_a_crash_after_the_sells_must_not_lose_the_proceeds(env):
    seed_sleeve(env)
    env.broker.crash_on = ("DBC", "BUY")                  # the SPY sale fills, then the process dies
    with pytest.raises(Crash):
        cycle(env)
    assert env.broker.positions == {} and u(env)["last_rebalance_month"] == "2026-10"      # nothing was saved
    env.broker.crash_on = None
    cycle(env)                                           # the restarted engine tries again
    assert u(env)["last_rebalance_month"] == MKEY
    assert sleeve_value(env) == pytest.approx(SLEEVE, abs=1.0), "the $1,000 of sale proceeds vanished from the sleeve"
    assert sum(env.broker.positions.values()) * 100.0 == pytest.approx(SLEEVE, abs=2.0)    # and was reinvested


def test_a_crash_after_one_buy_must_neither_lose_nor_invent_money(env):
    seed_sleeve(env)
    env.broker.crash_on = ("EEM", "BUY")                  # SPY sold, DBC bought, then the process dies
    with pytest.raises(Crash):
        cycle(env)
    assert set(env.broker.positions) == {"DBC"}
    env.broker.crash_on = None
    cycle(env)
    assert u(env)["last_rebalance_month"] == MKEY
    assert sleeve_value(env) == pytest.approx(SLEEVE, abs=1.0)
    assert set(env.broker.positions) == {"DBC", "EEM", "QQQ"}
    assert sum(env.broker.positions.values()) * 100.0 == pytest.approx(SLEEVE, abs=2.0)


def test_a_crash_before_anything_is_sent_changes_nothing(env):
    seed_sleeve(env)
    env.broker.crash_on = ("SPY", "SELL")
    with pytest.raises(Crash):
        cycle(env)
    env.broker.crash_on = None
    cycle(env)
    assert sleeve_value(env) == pytest.approx(SLEEVE, abs=1.0) and u(env)["last_rebalance_month"] == MKEY


def test_the_saved_baseline_matches_the_account_after_a_normal_rebalance(env):
    seed_sleeve(env)
    cycle(env)
    assert u(env)["broker_cash_at_save"] == pytest.approx(env.broker.cash, abs=1e-6)
    assert u(env)["cash"] == pytest.approx(SLEEVE - sum(env.broker.positions.values()) * 100.0, abs=1e-6)


def test_a_fresh_sleeve_saves_a_baseline_too(env):
    env.broker.positions = {}
    cycle(env)
    assert len(env.broker.submitted) == 3 and u(env)["broker_cash_at_save"] == pytest.approx(env.broker.cash, abs=1e-6)
    assert u(env)["cash"] == pytest.approx(5000.0 - 5000.0, abs=1e-6)


def test_a_late_fill_that_was_never_booked_is_picked_up_next_cycle(env):
    seed_sleeve(env)
    env.broker.cash += 400.0                              # e.g. an order that filled after the engine gave up on it
    env.broker.positions = {}
    eng._us_reconcile_cash(u(env))
    assert u(env)["cash"] == pytest.approx(1100.0) and u(env)["broker_cash_at_save"] == pytest.approx(99400.0)
    assert eng._us_reconcile_cash(u(env)) is None and u(env)["cash"] == pytest.approx(1100.0)   # idempotent


def test_reconciliation_also_refreshes_the_holdings_from_the_broker(env):
    seed_sleeve(env)
    env.broker.positions = {"QQQ": 3.0}
    eng._us_reconcile_cash(u(env))
    assert u(env)["holdings"] == {"QQQ": 3.0}


# ---------------------------------------------------------------- the guards
def test_cash_that_moved_outside_the_engine_blocks_everything_and_alerts(env):
    seed_sleeve(env)
    env.broker.cash = 50000.0                             # e.g. the paper account was reset
    cycle(env)
    assert env.broker.submitted == [] and "us-paper-cash" in keys(env)
    assert "paper cash needs review" in eng.US_STATUS["note"] and u(env)["last_rebalance_month"] == "2026-10"


def test_unreadable_account_cash_blocks_the_cycle(env):
    seed_sleeve(env)
    env.broker.balance_override = {"source": "error", "cash": None}
    cycle(env)
    assert env.broker.submitted == [] and "us-paper-cash" in keys(env)


@pytest.mark.parametrize("bad,baseline", [({"source": "live", "cash": "nan"}, 99000.0),
                                           ({"source": "live", "cash": "inf"}, float("inf")),
                                           ({"source": "live", "cash": "-inf"}, float("-inf")),
                                           ({"source": "live"}, 99000.0),
                                           ({"source": "no_credentials", "cash": "0.00"}, 0.0),
                                           ({"source": "error", "cash": "99000"}, 99000.0),
                                           ({}, 99000.0)])
def test_every_kind_of_unusable_balance_is_unreadable(env, bad, baseline):
    """The baseline is chosen so the number itself would look acceptable: only the readability checks can refuse it."""
    seed_sleeve(env, baseline=baseline)
    env.broker.balance_override = bad
    reason = eng._us_reconcile_cash(u(env))
    assert reason is not None and "could not be read" in reason


def test_a_fresh_sleeve_will_not_start_blind(env):
    env.broker.positions = {}
    env.broker.balance_override = {"source": "error", "cash": None}
    cycle(env)
    assert env.broker.submitted == [] and "us-paper-cash" in keys(env)


def test_state_from_before_this_check_is_trusted_once_and_given_a_baseline(env):
    seed_sleeve(env, baseline=None)
    cycle(env)
    assert u(env)["last_rebalance_month"] == MKEY and u(env)["broker_cash_at_save"] == pytest.approx(env.broker.cash)


def test_a_small_cash_difference_such_as_a_dividend_is_absorbed(env):
    seed_sleeve(env)
    env.broker.cash += 3.5
    assert eng._us_reconcile_cash(u(env)) is None and u(env)["cash"] == pytest.approx(703.5)


def test_the_limit_is_100_dollars_or_half_the_allocation_whichever_is_larger(env):
    seed_sleeve(env)
    env.broker.cash = 99000.0 + 2499.0
    assert eng._us_reconcile_cash(u(env)) is None
    seed_sleeve(env)
    env.broker.cash = 99000.0 + 2501.0
    assert eng._us_reconcile_cash(u(env)) is not None


def test_shadow_and_live_modes_never_touch_the_account(env):
    seed_sleeve(env)
    env.mp.setattr(eng, "US_EXECUTE", False)
    assert eng._us_reconcile_cash(u(env)) is None and env.broker.balance_reads == 0
    env.mp.setattr(eng, "US_EXECUTE", True)
    env.mp.setattr(eng, "US_LIVE", True)
    assert eng._us_reconcile_cash(u(env)) is None and env.broker.balance_reads == 0
    env.mp.setattr(eng, "US_LIVE", False)
    u(env)["cash"] = None
    assert eng._us_reconcile_cash(u(env)) is None and env.broker.balance_reads == 0


def test_the_snapshot_uses_the_reconciled_cash(env):
    seed_sleeve(env)
    env.broker.positions = {"DBC": 10.0}
    env.broker.cash += 400.0
    u(env)["last_snapshot_date"] = None
    cycle(env, now=datetime.datetime(2026, 11, 2, 16, 5, tzinfo=eng.NY), is_open=False)
    assert u(env)["equity_history"][-1]["v"] == pytest.approx(700.0 + 400.0 + 1000.0)


# ---------------------------------------------------------------- E8: order ids across retries
def test_the_first_attempt_uses_attempt_zero_ids(env):
    env.broker.positions = {}
    cycle(env)
    assert env.broker.coids and all(c.endswith("-0") for c in env.broker.coids)
    assert env.broker.coids[0] == f"us-{MKEY}-DBC-BUY-0"


def test_a_retry_after_a_failed_sell_must_place_new_orders(env):
    seed_sleeve(env)
    env.broker.script = {("SPY", "SELL"): "canceled"}
    cycle(env)
    assert u(env)["last_rebalance_month"] == "2026-10" and u(env)["history"][-1]["complete"] is False
    env.broker.script = {}
    eng.US_STATUS["retry_after"] = 0.0
    cycle(env)                                           # same month, second attempt: must not reuse the old order id
    assert u(env)["last_rebalance_month"] == MKEY, "the retry re-read the cancelled order and never traded"
    assert set(env.broker.positions) == {"DBC", "EEM", "QQQ"}
    assert env.broker.coids[0].endswith("-0") and env.broker.coids[1].endswith("-1")


def test_the_attempt_number_counts_earlier_attempts_in_this_month_only(env):
    env.broker.positions = {}
    u(env)["history"] = [{"month": "2026-10"}, {"month": MKEY}, {"month": MKEY}]
    cycle(env)
    assert all(c.endswith("-2") for c in env.broker.coids)


def test_a_crash_keeps_the_attempt_number_so_a_restart_recovers_instead_of_duplicating(env):
    env.broker.positions = {}
    env.broker.crash_on = ("EEM", "BUY")
    with pytest.raises(Crash):
        cycle(env)
    first = list(env.broker.coids)
    assert first == [f"us-{MKEY}-DBC-BUY-0"] and u(env)["history"] == []
    env.broker.crash_on = None
    cycle(env)                                           # attempt number unchanged: ids are reproducible after a crash
    assert env.broker.coids[0] == first[0]


def test_the_first_sight_of_an_old_state_saves_the_baseline_immediately(env):
    """Without this the first rotation after the upgrade would still lose its proceeds in a crash."""
    seed_sleeve(env, baseline=None)
    cycle(env, is_open=False)                            # nothing to trade: the market is closed
    assert env.state[env.key]["broker_cash_at_save"] == pytest.approx(99000.0) and env.saves


def test_even_the_first_rotation_after_the_upgrade_survives_a_crash(env):
    seed_sleeve(env, baseline=None)                      # state written by the old engine
    env.broker.crash_on = ("DBC", "BUY")
    with pytest.raises(Crash):
        cycle(env)
    assert env.state[env.key]["broker_cash_at_save"] == pytest.approx(99000.0)     # saved before any order went out
    env.broker.crash_on = None
    cycle(env)
    assert sleeve_value(env) == pytest.approx(SLEEVE, abs=1.0) and u(env)["last_rebalance_month"] == MKEY


def test_a_failed_baseline_save_does_not_stop_the_engine(env):
    seed_sleeve(env, baseline=None)

    def boom(s):
        raise OSError("disk full")
    env.mp.setattr(eng, "save_state", boom)
    assert eng._us_reconcile_cash(u(env)) is None and u(env)["broker_cash_at_save"] == pytest.approx(99000.0)
