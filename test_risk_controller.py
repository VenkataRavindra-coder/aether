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

# Input validation and fail-closed config
t("unknown size blocked", not ok("us", "SPY", "BUY", None, False, False))
t("non-numeric size blocked", not ok("us", "SPY", "BUY", "abc", False, False))
t("bad side blocked", not ok("us", "SPY", "HOLD", 10, False, False))
(d / "risk.json").unlink()  # fresh budget so the next test isolates the cap rule
os.environ["RISK_MAX_ORDER_USD_LIVE"] = "abc"
t("malformed live cap fails closed", not ok("us", "SPY", "BUY", 1, False, True))
os.environ["RISK_MAX_ORDER_USD_LIVE"] = "60"
(d / "risk.json").write_text("{corrupt")
t("corrupt budget file fails closed", not ok("us", "SPY", "BUY", 1, False, True))

fails = [n for n, c in results if not c]
print(f"\n{len(results) - len(fails)}/{len(results)} passed")
raise SystemExit(1 if fails else 0)
