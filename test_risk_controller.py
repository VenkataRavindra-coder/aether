import os, pathlib, tempfile
halt = pathlib.Path(tempfile.mkdtemp()) / "HALT"
os.environ.update(AETHER_HALT_FILE=str(halt), RISK_LOG="0", RISK_MAX_ORDER_USD="100")
import risk_controller as rc

ok = lambda *a: rc.check(*a)[0]
assert ok("us", "SPY", "BUY", 50, False, False)
assert not ok("us", "SPY", "BUY", 150, False, False)            # over cap
assert not ok("crypto", "BTC/USD", "BUY", 50, False, True)      # crypto live blocked
assert ok("crypto", "BTC/USD", "SELL", 500, True, True)         # exits always allowed
assert not ok("us", "SPY", "BUY", None, False, False)           # unknown size
assert not ok("us", "SPY", "HOLD", 50, False, False)            # bad side
halt.touch()
assert not ok("us", "SPY", "BUY", 10, False, False)             # halted
assert ok("us", "SPY", "SELL", 10, True, False)                 # exit still works
halt.unlink()
os.environ["RISK_MAX_ORDER_USD"] = "abc"
assert not ok("us", "SPY", "BUY", 1, False, False)              # bad config fails closed
os.environ["RISK_MAX_ORDER_USD"] = ""
assert ok("us", "SPY", "BUY", 5000, False, False)               # unset = no cap in v1
print("risk_controller: all 10 checks passed")
