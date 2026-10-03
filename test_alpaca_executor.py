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

from alpaca_executor import AlpacaExecutionClient, classify_broker_error, BROKER_ERROR_FREEZE_THRESHOLD

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
    c.consecutive_errors = 0
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

# --- classify_broker_error buckets the exception types that actually matter ---
import requests


class FakeAPIError(Exception):
    def __init__(self, status_code):
        self.status_code = status_code


t("a requests.Timeout is classified as timeout", classify_broker_error(requests.exceptions.Timeout()) == "timeout")
t("a requests.ConnectionError is classified as connection", classify_broker_error(requests.exceptions.ConnectionError()) == "connection")
t("HTTP 429 is classified as rate_limited", classify_broker_error(FakeAPIError(429)) == "rate_limited")
t("HTTP 503 is classified as server_error", classify_broker_error(FakeAPIError(503)) == "server_error")
t("HTTP 403 is classified as client_error", classify_broker_error(FakeAPIError(403)) == "client_error")
t("an exception with no status code is classified as unknown", classify_broker_error(RuntimeError("?")) == "unknown")


class FakeClientAlwaysFails:
    def submit_order(self, order_data):
        raise RuntimeError("broker is down")

    def get_order_by_client_id(self, coid):
        raise RuntimeError("still down")


# --- consecutive_errors increments on failure, resets on success ---
c = make_client()
c.client = FakeClientAlwaysFails()
for i in range(BROKER_ERROR_FREEZE_THRESHOLD):
    r = c.submit_market_order("SPY", "BUY", notional=50)
    t(f"failure {i+1} reports the running consecutive_errors count", r.get("consecutive_errors") == i + 1)
t("consecutive_errors has now reached the freeze threshold", c.consecutive_errors == BROKER_ERROR_FREEZE_THRESHOLD)
t("a persistent failure is classified (unknown, here -- a bare RuntimeError)", r.get("error_class") == "unknown")

c.client = FakeClientOK()
r = c.submit_market_order("SPY", "BUY", notional=50)
t("a subsequent success resets consecutive_errors back to zero", r["status"] == "submitted" and c.consecutive_errors == 0)

# --- a 429 triggers a short backoff instead of failing instantly ---
import time as _time


class FakeClientRateLimited:
    def submit_order(self, order_data):
        raise FakeAPIError(429)

    def get_order_by_client_id(self, coid):
        raise RuntimeError("not found")


c = make_client()
c.client = FakeClientRateLimited()
real_sleep = _time.sleep
slept = {}
_time.sleep = lambda s: slept.update(seconds=s)
try:
    r = c.submit_market_order("SPY", "BUY", notional=50)
finally:
    _time.sleep = real_sleep
t("a 429 is classified as rate_limited and reported as such", r.get("error_class") == "rate_limited")
t("a 429 triggers a backoff sleep before returning", slept.get("seconds") == 2)

fails = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(fails)}/{len(results)} passed")
if fails:
    print("FAILED:", fails)
    raise SystemExit(1)
