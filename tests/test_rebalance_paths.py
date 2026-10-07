"""AETHER rebalance and cycle path tests (plan D10, E2, E3b, E4, E5/E7 logic, price guard C8/D6, B-layer logic).

Run from the repo root with the service's interpreter:
    venv/bin/python3 -m pytest -q tests/test_rebalance_paths.py

Fakes only: the broker, Telegram, the decision log, the state file, signals and the clock are replaced in
memory for each test. Nothing is sent, nothing is traded, nothing real is written.
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
TODAY, MKEY = "2026-11-02", "2026-11"
NOW_ET = datetime.datetime(2026, 11, 2, 15, 35, tzinfo=eng.NY)  # Monday after the October month-end
PRICES = {s: 100.0 for s in UNIVERSE}


def make_sig():
    return {"weights": dict(WEIGHTS), "scores": {s: 0.1 for s in us.RISK_ETFS}, "selected": ["DBC", "EEM", "QQQ"],
            "signal_date": "2026-10-30", "latest_close": {s: 100.0 for s in UNIVERSE}}


class FakeBroker:
    paper = True
    client = object()

    def __init__(self, script=None, positions=None, open_syms=None):
        self.script = script or {}
        self.positions = positions
        self.open = set() if open_syms is None else open_syms
        self.consecutive_errors = 0
        self.submitted = []
        self._orders = {}

    def submit_market_order(self, symbol, side, qty=None, notional=None, client_order_id=None, strategy="us"):
        mode = self.script.get((symbol, side), "filled")
        if mode == "error":
            self.consecutive_errors += 1
            return {"status": "error", "message": "boom"}
        self.consecutive_errors = 0
        oid = f"{symbol}-{side}-{len(self.submitted)}"
        self.submitted.append((symbol, side))
        self._orders[oid] = (symbol, side, qty, notional, mode)
        return {"status": "submitted", "order_id": oid}

    def wait_for_fill(self, order_id, timeout=90.0, poll=2.0):
        symbol, side, qty, notional, mode = self._orders[order_id]
        full = qty if side == "SELL" else notional / 100.0
        if mode in ("filled", "partial_canceled"):
            q = full if mode == "filled" else full / 2
            pos = self.positions
            pos[symbol] = pos.get(symbol, 0.0) + (q if side == "BUY" else -q)
            if pos[symbol] <= 1e-9:
                del pos[symbol]
            return {"state": "filled" if mode == "filled" else "canceled", "filled_qty": q, "avg_price": 100.0}
        if mode in ("canceled", "rejected", "expired"):
            return {"state": mode, "filled_qty": 0.0, "avg_price": None}
        return {"state": "new", "filled_qty": 0.0, "avg_price": None}  # "stuck"

    def cancel_order(self, order_id):
        return True

    def get_positions_map(self):
        return None if self.positions is None else dict(self.positions)

    def open_order_symbols(self):
        return self.open

    def account_id(self):
        return "ACC-1"

    def get_account_balance(self):
        return {"source": "paper", "cash": "0", "portfolio_value": "0"}


@pytest.fixture
def env(monkeypatch):
    sent, logged, saves = [], [], []
    key = eng._us_state_key()
    state = {key: eng.default_us_state(), "other": {}}
    broker = FakeBroker(positions={})
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
    monkeypatch.setattr(eng, "_us_holdings", lambda us_: None if broker.positions is None else
                        {s: q for s, q in broker.positions.items() if s in UNIVERSE and q > 1e-9})
    monkeypatch.setattr(eng, "us_signals", lambda fetch, today: copy.deepcopy(sig))
    monkeypatch.setattr(eng, "load_state", lambda: state)

    def fake_save(s):
        snap = copy.deepcopy(s)
        saves.append(snap)
        state.clear()
        state.update(snap)

    monkeypatch.setattr(eng, "save_state", fake_save)
    return types.SimpleNamespace(sent=sent, logged=logged, saves=saves, state=state, key=key, broker=broker,
                                 sig=sig, mp=monkeypatch)


def u(env):
    return env.state[env.key]


def keys(env):
    return [s.get("key") for s in env.sent]


def rebalance(env, prices=None):
    p = PRICES if prices is None else prices
    eng._us_rebalance(u(env), TODAY, MKEY, NOW_ET, None, lambda symbols: {s: p[s] for s in symbols if s in p})


def cycle(env, now=NOW_ET, is_open=True, prices=None):
    p = PRICES if prices is None else prices
    eng._us_cycle_inner(lambda syms: {}, lambda symbols: {s: p[s] for s in symbols if s in p}, lambda: (is_open, now))


def decisions(env):
    return [k.get("decision") for a, k in env.logged]


# ---------------------------------------------------------------- the happy path
def test_fresh_account_buys_the_three_etfs_and_records_the_month(env):
    rebalance(env)
    assert sorted(env.broker.submitted) == [("DBC", "BUY"), ("EEM", "BUY"), ("QQQ", "BUY")]
    st = u(env)
    assert st["last_rebalance_month"] == MKEY and st["frozen"] is None
    assert set(st["holdings"]) == {"DBC", "EEM", "QQQ"}
    assert st["history"][-1]["complete"] is True and eng.US_STATUS["note"] == "rebalanced"
    assert all(k is None for k in keys(env))  # a summary message, no alerts


# ---------------------------------------------------------------- refusals: nothing is sent
def test_frozen_engine_sends_nothing_and_alerts(env):
    u(env)["frozen"] = "unreconciled order(s) ['x']"
    rebalance(env)
    assert env.broker.submitted == [] and "us-frozen" in keys(env)


def test_retry_window_is_respected(env):
    eng.US_STATUS["retry_after"] = time.time() + 600
    rebalance(env)
    assert env.broker.submitted == [] and eng.US_STATUS["note"] == "waiting to retry a failed rebalance"


def test_signal_failure_sends_nothing_and_alerts(env):  # D10
    def boom(fetch, today):
        raise ValueError("stale data: newest completed-month bar is 2026-10-12")
    env.mp.setattr(eng, "us_signals", boom)
    rebalance(env)
    assert env.broker.submitted == [] and "us-signal" in keys(env)
    assert "stale data" in eng.US_STATUS["last_error"] and u(env)["last_rebalance_month"] is None


def test_unreadable_broker_positions_sends_nothing(env):
    env.broker.positions = None
    rebalance(env)
    assert env.broker.submitted == [] and "us-pos" in keys(env)


@pytest.mark.parametrize("open_syms", [{"SPY"}, None])
def test_open_orders_on_strategy_etfs_or_unreadable_wait(env, open_syms):
    env.broker.open = open_syms
    rebalance(env)
    assert env.broker.submitted == [] and "us-open" in keys(env)


def test_open_order_on_a_non_strategy_symbol_does_not_block(env):
    env.broker.open = {"AAPL"}
    rebalance(env)
    assert len(env.broker.submitted) == 3


def test_unknown_existing_positions_are_not_taken_over(env):  # E3b
    env.broker.positions = {"SPY": 3.0}
    assert u(env)["cash"] is None
    rebalance(env)
    assert env.broker.submitted == [] and "us-preexisting" in keys(env)


def test_known_state_with_positions_is_allowed(env):
    env.broker.positions = {"SPY": 3.0}
    u(env)["cash"] = 700.0
    rebalance(env)
    assert ("SPY", "SELL") in env.broker.submitted


def test_a_missing_price_sends_nothing(env):
    rebalance(env, prices={k: v for k, v in PRICES.items() if k != "DBC"})
    assert env.broker.submitted == [] and "us-price" in keys(env)


# ---------------------------------------------------------------- price-sanity guard (C8/D6)
def test_a_spiked_price_blocks_the_rebalance(env):
    rebalance(env, prices={**PRICES, "QQQ": 130.0})
    assert env.broker.submitted == [] and "us-price-sanity" in keys(env)
    assert "price_sanity" in decisions(env) and u(env)["last_rebalance_month"] is None
    assert "QQQ" in env.sent[0]["text"]


def test_a_small_price_difference_is_fine(env):
    rebalance(env, prices={**PRICES, "QQQ": 105.0})
    assert len(env.broker.submitted) == 3


def test_missing_reference_closes_block_the_rebalance(env):
    del env.sig["latest_close"]
    rebalance(env)
    assert env.broker.submitted == [] and "us-price-sanity" in keys(env)


# ---------------------------------------------------------------- halt, pre-flight, approval gate (E1, E2)
def test_halt_blocks_a_rotation_that_has_orders(env):
    env.mp.setattr(eng.risk_controller, "halted", lambda: True)
    rebalance(env)
    assert env.broker.submitted == [] and "us-halted" in keys(env)
    assert "halted" in decisions(env) and u(env)["last_rebalance_month"] is None


def test_halt_does_not_block_a_rebalance_that_needs_no_orders(env):
    env.broker.positions = {s: 5000 / 3 / 100.0 for s in ("DBC", "EEM", "QQQ")}
    u(env)["cash"] = 0.0
    env.mp.setattr(eng.risk_controller, "halted", lambda: True)
    rebalance(env)
    assert env.broker.submitted == [] and u(env)["last_rebalance_month"] == MKEY


def test_preflight_failure_sends_nothing(env):  # E2: all-or-nothing
    env.mp.setattr(eng.risk_controller, "preflight", lambda engine, orders: (False, "over_max_order_usd_live(100)"))
    rebalance(env)
    assert env.broker.submitted == [] and "us-preflight" in keys(env)
    assert "preflight_failed" in decisions(env) and u(env)["last_rebalance_month"] is None


def test_the_preflight_sees_the_whole_plan(env):
    seen = []
    env.mp.setattr(eng.risk_controller, "preflight", lambda engine, orders: (seen.append((engine, orders)), (True, "ok"))[1])
    rebalance(env)
    assert seen[0][0] == "us" and len(seen[0][1]) == 3 and all(o[0] == "BUY" for o in seen[0][1])


def test_a_refusing_approval_gate_sends_nothing_and_saves_nothing(env):
    env.mp.setattr(eng, "_us_gate_active", lambda: True)
    env.mp.setattr(eng, "_us_live_gate", lambda *a, **k: False)
    rebalance(env)
    assert env.broker.submitted == [] and env.saves == []


# ---------------------------------------------------------------- failures during execution (E5/E7 logic)
def seeded(env, positions=None, cash=0.0):
    env.broker.positions = {"SPY": 10.0} if positions is None else positions
    u(env)["cash"] = cash


def test_failed_sell_skips_the_buys_and_keeps_the_month_open(env):
    seeded(env)
    env.broker.script = {("SPY", "SELL"): "canceled"}
    rebalance(env)
    assert env.broker.submitted == [("SPY", "SELL")]
    assert u(env)["last_rebalance_month"] is None and u(env)["history"][-1]["complete"] is False
    assert "us-fail" in keys(env) and eng.US_STATUS["retry_after"] > time.time()


def test_unreconciled_order_freezes_the_engine(env):
    seeded(env)
    env.broker.script = {("SPY", "SELL"): "stuck"}
    rebalance(env)
    assert "unreconciled" in u(env)["frozen"] and u(env)["last_rebalance_month"] is None


def test_repeated_broker_errors_freeze_the_engine(env):
    env.broker.script = {(s, "BUY"): "error" for s in ("DBC", "EEM", "QQQ")}
    rebalance(env)
    assert "consecutive broker errors" in u(env)["frozen"] and u(env)["last_rebalance_month"] is None


def test_a_retry_after_a_failed_sell_completes_without_duplicate_buys(env):
    seeded(env)
    env.broker.script = {("SPY", "SELL"): "canceled"}
    rebalance(env)
    assert env.broker.submitted == [("SPY", "SELL")]            # no buys in the failed attempt
    env.broker.script = {}
    eng.US_STATUS["retry_after"] = 0.0
    rebalance(env)
    assert env.broker.submitted.count(("DBC", "BUY")) == 1      # each buy exactly once
    assert u(env)["last_rebalance_month"] == MKEY
    assert set(env.broker.positions) == {"DBC", "EEM", "QQQ"} and "SPY" not in env.broker.positions


# ---------------------------------------------------------------- the cycle (E4: month boundaries and restarts)
def test_cycle_does_nothing_while_the_market_is_closed(env):
    calls = []
    env.mp.setattr(eng, "_us_rebalance", lambda *a, **k: calls.append(1))
    cycle(env, is_open=False)
    assert calls == [] and eng.US_STATUS["note"] == "US market closed"


def test_cycle_skips_a_month_that_is_already_done(env):
    calls = []
    env.mp.setattr(eng, "_us_rebalance", lambda *a, **k: calls.append(1))
    u(env)["last_rebalance_month"] = MKEY
    cycle(env)
    assert calls == [] and eng.US_STATUS["note"] == f"already rebalanced for {MKEY}"


def test_cycle_waits_until_1530_et(env):
    calls = []
    env.mp.setattr(eng, "_us_rebalance", lambda *a, **k: calls.append(1))
    cycle(env, now=datetime.datetime(2026, 11, 2, 11, 0, tzinfo=eng.NY))
    assert calls == [] and eng.US_STATUS["note"] == "waiting until 15:30 ET to rebalance"


def test_cycle_rebalances_once_when_open_after_1530_in_a_new_month(env):
    calls = []
    env.mp.setattr(eng, "_us_rebalance", lambda us_, today, mkey, *a, **k: calls.append((today, mkey)))
    cycle(env)
    assert calls == [(TODAY, MKEY)]


def test_a_restart_across_the_month_end_rebalances_the_new_month_only(env):
    calls = []
    env.mp.setattr(eng, "_us_rebalance", lambda us_, today, mkey, *a, **k: calls.append(mkey))
    u(env)["last_rebalance_month"] = "2026-10"
    cycle(env, now=datetime.datetime(2026, 10, 30, 15, 40, tzinfo=eng.NY))     # still October: done
    cycle(env, now=NOW_ET)                                                      # November: due
    assert calls == ["2026-11"]


def test_exactly_one_rebalance_per_month_across_repeated_cycles(env):
    cycle(env)
    assert len(env.broker.submitted) == 3
    cycle(env)
    cycle(env)
    assert len(env.broker.submitted) == 3 and eng.US_STATUS["note"] == f"already rebalanced for {MKEY}"


def test_a_failed_rebalance_retries_only_after_the_window(env):
    seeded(env)
    env.broker.script = {("SPY", "SELL"): "canceled"}
    cycle(env)
    assert env.broker.submitted == [("SPY", "SELL")]
    cycle(env)
    assert env.broker.submitted == [("SPY", "SELL")] and eng.US_STATUS["note"] == "waiting to retry a failed rebalance"
    env.broker.script = {}
    eng.US_STATUS["retry_after"] = 0.0
    cycle(env)
    assert u(env)["last_rebalance_month"] == MKEY


# ---------------------------------------------------------------- the daily equity snapshot (feeds the drawdown kill)
AFTER_CLOSE = datetime.datetime(2026, 11, 2, 16, 5, tzinfo=eng.NY)


def test_snapshot_is_written_once_per_day_after_the_close(env):
    env.broker.positions = {"DBC": 10.0}
    u(env).update(cash=100.0, holdings={"DBC": 10.0}, last_rebalance_month=MKEY)
    cycle(env, now=AFTER_CLOSE, is_open=False)
    assert len(u(env)["equity_history"]) == 1 and u(env)["equity_history"][0]["v"] == 1100.0
    assert u(env)["last_snapshot_date"] == TODAY
    cycle(env, now=AFTER_CLOSE, is_open=False)
    assert len(u(env)["equity_history"]) == 1


def test_no_snapshot_while_the_market_is_open_or_before_the_close(env):
    env.broker.positions = {"DBC": 10.0}
    u(env).update(cash=100.0, holdings={"DBC": 10.0}, last_rebalance_month=MKEY)
    cycle(env, now=NOW_ET, is_open=True)
    cycle(env, now=datetime.datetime(2026, 11, 2, 9, 0, tzinfo=eng.NY), is_open=False)
    assert u(env)["equity_history"] == []


def test_no_snapshot_when_cash_is_unknown(env):
    cycle(env, now=AFTER_CLOSE, is_open=False)
    assert u(env)["equity_history"] == []
