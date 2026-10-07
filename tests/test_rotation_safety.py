"""AETHER rotation-safety tests (plan E-layer): what _us_execute_paper does when a SELL fails.

Run from the repo root with the service's interpreter:
    venv/bin/python3 -m pytest -q tests/test_rotation_safety.py

Uses a FAKE broker (no network, no keys, no Telegram). It replaces aether_engine.us_broker for the
duration of each test only.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest

import aether_engine as eng

PRICES = {"SPY": 100.0, "QQQ": 100.0, "GLD": 100.0, "TLT": 100.0}


class FakeBroker:
    paper = True

    def __init__(self, script=None, positions=None):
        self.script = script or {}
        self.positions = {} if positions is None else positions
        self.submitted = []
        self._orders = {}

    def submit_market_order(self, symbol, side, qty=None, notional=None, client_order_id=None, strategy="us"):
        mode = self.script.get((symbol, side), "filled")
        if mode == "error":
            return {"status": "error", "message": "boom"}
        oid = f"{symbol}-{side}-{len(self.submitted)}"
        self.submitted.append((symbol, side))
        self._orders[oid] = (symbol, side, qty, notional, mode)
        return {"status": "submitted", "order_id": oid}

    def wait_for_fill(self, order_id, timeout=90.0, poll=2.0):
        symbol, side, qty, notional, mode = self._orders[order_id]
        full = qty if side == "SELL" else notional / 100.0
        if mode == "filled":
            return {"state": "filled", "filled_qty": full, "avg_price": 100.0}
        if mode in ("canceled", "rejected", "expired"):
            return {"state": mode, "filled_qty": 0.0, "avg_price": None}
        if mode == "partial_canceled":
            return {"state": "canceled", "filled_qty": full / 2, "avg_price": 100.0}
        if mode == "stuck":
            return {"state": "new", "filled_qty": 0.0, "avg_price": None}
        raise AssertionError(mode)

    def cancel_order(self, order_id):
        return True

    def get_positions_map(self):
        return self.positions


def run(orders, script=None):
    broker = FakeBroker(script)
    eng.us_broker = broker
    results, book, cash = eng._us_execute_paper(orders, PRICES, 1000.0, {"SPY": 5.0}, mkey="2026-11")
    return broker, results


SELL_SPY = {"symbol": "SPY", "side": "SELL", "qty": 2.0, "est_value": 200.0}
BUY_QQQ = {"symbol": "QQQ", "side": "BUY", "notional": 150.0, "est_value": 150.0}
BUY_GLD = {"symbol": "GLD", "side": "BUY", "notional": 50.0, "est_value": 50.0}


@pytest.fixture(autouse=True)
def restore_broker():
    saved = eng.us_broker
    yield
    eng.us_broker = saved


def states(results):
    return {(r["symbol"], r["side"]): r["state"] for r in results}


# ---- a SELL that does not fully fill must stop the BUYs that were sized on its proceeds
@pytest.mark.parametrize("mode", ["error", "canceled", "rejected", "expired", "partial_canceled"])
def test_failed_sell_blocks_buys(mode):
    broker, results = run([SELL_SPY, BUY_QQQ, BUY_GLD], {("SPY", "SELL"): mode})
    assert ("QQQ", "BUY") not in broker.submitted
    assert ("GLD", "BUY") not in broker.submitted
    st = states(results)
    assert st[("QQQ", "BUY")] == "skipped_after_failed_sell"
    assert st[("GLD", "BUY")] == "skipped_after_failed_sell"


def test_skipped_buys_count_as_failed_so_the_month_stays_incomplete():
    _, results = run([SELL_SPY, BUY_QQQ], {("SPY", "SELL"): "canceled"})
    failed = [r for r in results if r["state"] not in ("filled", "simulated")]
    assert len(failed) == 2  # the canceled sell and the skipped buy


def test_filled_sell_lets_buys_proceed():
    broker, results = run([SELL_SPY, BUY_QQQ, BUY_GLD])
    assert broker.submitted == [("SPY", "SELL"), ("QQQ", "BUY"), ("GLD", "BUY")]
    assert all(r["state"] == "filled" for r in results)


def test_other_sells_are_still_attempted_after_a_failed_sell():
    second = {"symbol": "TLT", "side": "SELL", "qty": 1.0, "est_value": 100.0}
    broker, results = run([SELL_SPY, second, BUY_QQQ], {("SPY", "SELL"): "error"})
    assert ("TLT", "SELL") in broker.submitted      # selling more only reduces risk
    assert ("QQQ", "BUY") not in broker.submitted


def test_failed_buy_does_not_block_other_buys():
    broker, results = run([SELL_SPY, BUY_QQQ, BUY_GLD], {("QQQ", "BUY"): "error"})
    assert ("GLD", "BUY") in broker.submitted       # only sells gate buys


def test_buys_only_plan_unaffected():
    broker, results = run([BUY_QQQ, BUY_GLD])
    assert broker.submitted == [("QQQ", "BUY"), ("GLD", "BUY")]


def test_unreconciled_sell_still_skips_the_rest_as_before():
    broker, results = run([SELL_SPY, BUY_QQQ], {("SPY", "SELL"): "stuck"})
    st = states(results)
    assert st[("SPY", "SELL")] == "unreconciled"
    assert st[("QQQ", "BUY")] == "skipped_after_unreconciled"
    assert ("QQQ", "BUY") not in broker.submitted
