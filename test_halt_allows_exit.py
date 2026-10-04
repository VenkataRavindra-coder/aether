"""Proves, against the real submit_market_order() code path (not just risk_controller in
isolation -- test_risk_controller.py already covers that), that AETHER_HALT blocks a new US
entry but never blocks a closing exit. This is the exact scenario the aether_safety package's
BUILD.md asks to verify on paper: touch ~/AETHER_HALT -> a SELL that closes a held position
still reaches the broker -> a BUY does not. Here it's done offline with a fake broker client
(no real orders, no market-hours dependency) so it runs anywhere, anytime, and stays in CI.
Run: python3 test_halt_allows_exit.py
"""
import os
import tempfile

tmpdir = tempfile.mkdtemp()
halt_path = os.path.join(tmpdir, "HALT")
os.environ.update(
    RISK_LOG="0",
    AETHER_HALT_FILE=halt_path,
    AETHER_RISK_STATE=os.path.join(tmpdir, "risk.json"),
)

import risk_controller
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
    def submit_order(self, order_data):
        return FakeOrder("order-1")


def make_client():
    c = AlpacaExecutionClient.__new__(AlpacaExecutionClient)
    c.paper = True
    c.api_key, c.api_secret, c.live_trading = "k", "s", False
    c.consecutive_errors = 0
    c.client = FakeClientOK()
    return c


risk_controller.set_kill("us", False)

t("halt file does not exist yet", not risk_controller.halted())

c = make_client()
r = c.submit_market_order("IWM", "BUY", notional=50)
t("without a halt, a normal BUY (new entry) is allowed", r["status"] == "submitted")

with open(halt_path, "w") as f:
    f.write("manual halt test\n")
t("halt file now exists", risk_controller.halted())

c = make_client()
r = c.submit_market_order("IWM", "BUY", notional=50)
t("with AETHER_HALT present, a new entry (BUY) is blocked", r["status"] == "error" and "halted" in r.get("message", ""))

c = make_client()
r = c.submit_market_order("IWM", "SELL", qty=5.82)
t("with AETHER_HALT present, a closing SELL still reaches the broker (fills)", r["status"] == "submitted")
t("...and it actually reached the fake broker's submit_order, not a stub", r["order_id"] == "order-1")

os.remove(halt_path)
t("halt file removed", not risk_controller.halted())

c = make_client()
r = c.submit_market_order("IWM", "BUY", notional=50)
t("after rm ~/AETHER_HALT, new entries work again", r["status"] == "submitted")

import shutil
shutil.rmtree(tmpdir, ignore_errors=True)

fails = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(fails)}/{len(results)} passed")
if fails:
    print("FAILED:", fails)
    raise SystemExit(1)
