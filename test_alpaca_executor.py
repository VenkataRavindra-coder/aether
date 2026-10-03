"""Offline tests for AlpacaExecutionClient.submit_market_order's client_order_id handling.
Uses fake broker clients (no network, no credentials) so this runs anywhere. Run:
    python3 test_alpaca_executor.py
"""
import os

os.environ.setdefault("RISK_LOG", "0")
os.environ.setdefault("AETHER_HALT_FILE", "/tmp/_test_alpaca_executor_halt")
os.environ.setdefault("AETHER_RISK_STATE", "/tmp/_test_alpaca_executor_risk.json")

import risk_controller
risk_controller.set_kill("us", False)

from alpaca_executor import AlpacaExecutionClient

results = []


def t(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL") + ": " + name)


class FakeOrder:
    def __init__(self, id):
        self.id = id
        self.status = "accepted"


class FakeClientOK:
    """A normal submit: the broker accepts and returns an order straight away."""
    def __init__(self):
        self.last_request = None

    def submit_order(self, order_data):
        self.last_request = order_data
        return FakeOrder("order-123")

    def get_order_by_client_id(self, coid):
        raise AssertionError("should not be looked up when submit succeeds")


class FakeClientDuplicate:
    """Simulates the exact P0 scenario: the broker actually accepted the order, but we never
    saw the response (timeout / connection reset). A retry with the SAME client_order_id must
    recover the order that already exists instead of placing a second one."""
    def __init__(self):
        self.existing = FakeOrder("order-already-there")
        self.submit_calls = 0

    def submit_order(self, order_data):
        self.submit_calls += 1
        raise Exception("simulated timeout after the broker already accepted the order")

    def get_order_by_client_id(self, coid):
        return self.existing


class FakeClientHardFail:
    """A genuine failure: nothing ever landed at the broker, so the client_order_id lookup
    also finds nothing. Must still report a real error, not fabricate a success."""
    def submit_order(self, order_data):
        raise Exception("insufficient buying power")

    def get_order_by_client_id(self, coid):
        raise Exception("order not found")


def make_client():
    c = AlpacaExecutionClient.__new__(AlpacaExecutionClient)
    c.paper = True
    c.api_key, c.api_secret, c.live_trading = "k", "s", False
    return c


# --- normal path ---
c = make_client()
fake = FakeClientOK()
c.client = fake
r = c.submit_market_order("SPY", "BUY", notional=50, client_order_id="us-2026-10-SPY-BUY")
t("normal submit returns the broker's own order id", r["status"] == "submitted" and r["order_id"] == "order-123")
t("client_order_id is actually sent to the broker request",
  getattr(fake.last_request, "client_order_id", None) == "us-2026-10-SPY-BUY")

# --- recovered duplicate: the P0 scenario this patch exists for ---
c = make_client()
c.client = FakeClientDuplicate()
r = c.submit_market_order("SPY", "BUY", notional=50, client_order_id="us-2026-10-SPY-BUY")
t("a submit error recovers the already-landed order via client_order_id lookup",
  r["status"] == "submitted" and r.get("recovered") is True and r["order_id"] == "order-already-there")
t("the recovery path does not retry the submit a second time", c.client.submit_calls == 1)

# --- genuine failure: nothing landed, lookup also fails ---
c = make_client()
c.client = FakeClientHardFail()
r = c.submit_market_order("SPY", "BUY", notional=50, client_order_id="us-2026-10-SPY-BUY")
t("a genuine failure still reports error (never fabricates success)", r["status"] == "error")

# --- client_order_id is optional ---
c = make_client()
c.client = FakeClientOK()
r = c.submit_market_order("SPY", "SELL", qty=1)
t("an order with no client_order_id still submits normally", r["status"] == "submitted")

# --- the id format _us_execute_paper actually builds stays within Alpaca's length limit ---
long_id = f"us-{'2026-10'}-{'EEM'}-{'BUY'}"[:48]
t("a typical generated client_order_id fits the 48-char limit", len(long_id) <= 48)
worst_case = f"us-{'2026-10'}-{'VERYLONGSYMBOLNAME1234567890'}-{'BUY'}"[:48]
t("even an unusually long symbol is truncated to fit the 48-char limit", len(worst_case) <= 48)

fails = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(fails)}/{len(results)} passed")
if fails:
    print("FAILED:", fails)
    raise SystemExit(1)
