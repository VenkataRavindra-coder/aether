"""Offline tests for risk_controller v2. Run: python3 test_risk_controller.py"""
import json
import os
import pathlib
import tempfile

d = pathlib.Path(tempfile.mkdtemp())
os.environ.update(AETHER_HALT_FILE=str(d / "HALT"), AETHER_RISK_STATE=str(d / "risk.json"),
                  RISK_LOG="0", RISK_MAX_ORDER_USD_LIVE="60", RISK_MAX_DAILY_BUY_USD_LIVE="100")
import risk_controller as rc

results = []


def t(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL") + ": " + name)


ok = lambda *a: rc.check(*a)[0]

# The US engine is registered in the kill-switch table too, so its entries fail closed until
# the US cycle has reported a state at least once since process start.
t("US blocked until its kill switch is evaluated", not ok("us", "SPY", "BUY", 10, False, False))
rc.set_kill("us", False)
t("US allowed once its kill switch is clear", ok("us", "SPY", "BUY", 10, False, False))

# Paper is never capped by the live limits
t("paper US buy of any size allowed", ok("us", "SPY", "BUY", 5000, False, False))

# Real-money caps
t("live US buy under both caps allowed", ok("us", "SPY", "BUY", 50, False, True))
t("live US buy over per-order cap blocked", not ok("us", "SPY", "BUY", 61, False, True))
t("live daily budget enforced (50 + 55 > 100)", not ok("us", "QQQ", "BUY", 55, False, True))
t("live budget remainder usable (50 + 50 = 100)", ok("us", "QQQ", "BUY", 50, False, True))
t("live budget persisted to disk", json.loads((d / "risk.json").read_text())["usd"] == 100)
t("blocked orders did not consume budget", not ok("us", "IWM", "BUY", 1, False, True))

# Crypto kill switch, which now covers the webhook too
t("crypto live always blocked", not ok("crypto", "BTC/USD", "BUY", 10, False, True))
t("crypto blocked until kill switch evaluated", not ok("crypto", "BTC/USD", "BUY", 10, False, False))
rc.set_kill("crypto", False)
t("crypto paper allowed once kill switch is clear", ok("crypto", "BTC/USD", "BUY", 10, False, False))
rc.set_kill("crypto", True)
t("daily-loss kill blocks crypto entries", not ok("crypto", "BTC/USD", "BUY", 10, False, False))
t("exits still allowed during kill", ok("crypto", "BTC/USD", "SELL", 10, True, False))
t("US unaffected by crypto kill", ok("us", "SPY", "BUY", 10, False, False))
rc.set_kill("crypto", False)

# Manual halt
(d / "HALT").touch()
t("halt blocks entries", not ok("us", "SPY", "BUY", 10, False, False))
t("halt still allows exits", ok("us", "SPY", "SELL", 10, True, True))
(d / "HALT").unlink()

# Plan-level pre-flight: all-or-nothing, and it must not consume the daily buy budget
(d / "risk.json").unlink(missing_ok=True)
t("preflight allows an affordable live plan",
  rc.preflight("us", [("SELL", 100, True, True), ("BUY", 40, False, True), ("BUY", 40, False, True)])[0])
t("preflight did not consume the daily budget",
  (not (d / "risk.json").exists()) or json.loads((d / "risk.json").read_text())["usd"] == 0)
t("preflight blocks a plan whose buy exceeds the per-order cap",
  not rc.preflight("us", [("SELL", 100, True, True), ("BUY", 61, False, True)])[0])
t("preflight blocks a plan that exceeds the daily budget",
  not rc.preflight("us", [("BUY", 50, False, True), ("BUY", 55, False, True)])[0])
t("preflight reports which rule blocked the plan",
  rc.preflight("us", [("BUY", 61, False, True)])[1].startswith("over_max_order_usd_live"))
t("preflight ignores exits when checking the caps",
  rc.preflight("us", [("SELL", 5000, True, True)])[0])

# The P0 scenario this patch exists to prevent: a plan whose SELLs would fill while the BUY is
# refused. Pre-flight must refuse the WHOLE plan, so a cap or a halt can never leave the account
# liquidated and out of its intended exposure.
(d / "risk.json").write_text(json.dumps({"day": rc._today(), "usd": 100}))
t("exhausted daily budget refuses the whole plan, not just the buy leg",
  not rc.preflight("us", [("SELL", 5000, True, True), ("BUY", 40, False, True)])[0])
t("...while the exit leg on its own is still allowed",
  rc.preflight("us", [("SELL", 5000, True, True)])[0])
(d / "risk.json").unlink(missing_ok=True)

# A halt must never let a rotation sell without re-buying
(d / "HALT").touch()
t("halt blocks a rotation's buy leg", not rc.preflight("us", [("BUY", 10, False, False)])[0])
t("halt does not block the exit leg", rc.preflight("us", [("SELL", 10, True, False)])[0])
(d / "HALT").unlink()

# US kill switch (new): the engine that can spend real money
rc.set_kill("us", True)
t("US kill switch blocks new entries", not ok("us", "SPY", "BUY", 10, False, False))
t("US kill switch still allows exits", ok("us", "SPY", "SELL", 10, True, False))
rc.set_kill("us", False)

# Input validation and fail-closed config
t("unknown size blocked", not ok("us", "SPY", "BUY", None, False, False))
t("non-numeric size blocked", not ok("us", "SPY", "BUY", "abc", False, False))
t("bad side blocked", not ok("us", "SPY", "HOLD", 10, False, False))
(d / "risk.json").unlink(missing_ok=True)  # fresh budget so the next test isolates the cap rule
os.environ["RISK_MAX_ORDER_USD_LIVE"] = "abc"
t("malformed live cap fails closed", not ok("us", "SPY", "BUY", 1, False, True))
os.environ["RISK_MAX_ORDER_USD_LIVE"] = "60"
(d / "risk.json").write_text("{corrupt")
t("corrupt budget file fails closed", not ok("us", "SPY", "BUY", 1, False, True))

fails = [n for n, c in results if not c]
print(f"\n{len(results) - len(fails)}/{len(results)} passed")
raise SystemExit(1 if fails else 0)
