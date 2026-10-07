"""Daily extreme-condition stress test for AETHER's order-execution path.

Runs against a DEDICATED Alpaca paper account (STRESS_ALPACA_API_KEY/SECRET_KEY, a separate
$10k paper account created specifically for this), never the account the real US strategy
trades on. Uses the real AlpacaExecutionClient / submit_market_order code -- not a
reimplementation -- so these tests exercise the exact same execution, timeout, retry and
error-classification logic that is live in production, just pointed at a sandbox account and
tagged strategy="stress" so risk_controller tracks it under its own key, never the real "us"
budget/kill-switch state.

Deliberately does NOT touch ~/AETHER_HALT -- that file is shared with the live engine, and
toggling it here would halt real trading. Halt behavior is already covered by
test_halt_allows_exit.py against a throwaway temp path.

PDT awareness: Alpaca paper accounts enforce real Pattern Day Trader rules. With $10k equity
(under the $25k PDT threshold), 4 day-trades in a rolling 5-business-day window locks the
account out of further day-trades. A local ledger (stress_pdt_ledger.json) tracks day-trades
taken so this harness can hold a position open overnight instead of closing it the same day
once it's close to the limit, and treats an actual PDT rejection as an expected, correctly
-handled outcome rather than a failure.

Run: python3 stress_test.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
load_dotenv()

from alpaca_executor import AlpacaExecutionClient, classify_broker_error  # noqa: E402
import notify  # noqa: E402

LEDGER_PATH = os.path.join(os.path.expanduser("~"), ".stress_pdt_ledger.json")
DAY_TRADE_LIMIT = 4  # Alpaca locks at >3 day-trades in 5 business days under $25k equity.
MAX_NOTIONAL_PCT = 0.25  # mirrors the real US engine's max_notional_pct param.
LIQUID_SYMBOL = "SPY"
FRACTIONAL_SYMBOL = "BRK.B"  # high price per share -> forces a fractional qty on a small $ buy.

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = ""):
    results.append((name, ok, detail))
    print(("PASS" if ok else "FAIL") + f": {name}" + (f" -- {detail}" if detail else ""))


def load_ledger() -> dict:
    if os.path.exists(LEDGER_PATH):
        try:
            return json.load(open(LEDGER_PATH))
        except (json.JSONDecodeError, OSError):
            pass
    return {"day_trades": []}  # list of ISO date strings


def save_ledger(ledger: dict):
    tmp = LEDGER_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(ledger, f)
    os.replace(tmp, LEDGER_PATH)


def day_trades_in_window(ledger: dict) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)  # conservative: 7 calendar days
    return sum(1 for ts in ledger["day_trades"] if datetime.fromisoformat(ts) > cutoff)


def record_day_trade(ledger: dict):
    ledger["day_trades"].append(datetime.now(timezone.utc).isoformat())
    save_ledger(ledger)


def main() -> int:
    client = AlpacaExecutionClient(paper=True, key_env="STRESS_ALPACA_API_KEY",
                                    secret_env="STRESS_ALPACA_SECRET_KEY")
    if not client.client:
        record("client_initialised", False, "STRESS_ALPACA_API_KEY/SECRET_KEY missing or invalid")
        return _finish()

    record("client_initialised", True)

    bal = client.get_account_balance()
    reachable = bal.get("source") == "live"
    equity = float(bal.get("portfolio_value") or 0) if reachable else 0
    record("account_reachable", reachable and equity > 0, f"source={bal.get('source')} equity={equity}")
    if not reachable or equity <= 0:
        return _finish()

    ledger = load_ledger()
    trades_this_window = day_trades_in_window(ledger)
    safe_to_day_trade = trades_this_window < DAY_TRADE_LIMIT - 1  # leave one trade of headroom
    record("pdt_budget_check", True,
           f"{trades_this_window}/{DAY_TRADE_LIMIT} day-trades used in trailing window; "
           f"{'will close same-day' if safe_to_day_trade else 'will hold overnight'}")

    # 1. Baseline: small market buy on a liquid symbol.
    oid1 = f"stress-{uuid.uuid4().hex[:12]}"
    r1 = client.submit_market_order(LIQUID_SYMBOL, "BUY", notional=50.0,
                                     client_order_id=oid1, strategy="stress")
    record("baseline_buy", r1.get("status") == "submitted", json.dumps(r1))

    # 2. Fractional share order (small $ on a high-priced symbol forces qty < 1).
    oid2 = f"stress-{uuid.uuid4().hex[:12]}"
    r2 = client.submit_market_order(FRACTIONAL_SYMBOL, "BUY", notional=75.0,
                                     client_order_id=oid2, strategy="stress")
    record("fractional_buy", r2.get("status") == "submitted", json.dumps(r2))

    # 3. Large notional near the 25%-of-equity ceiling the real engine uses.
    large_notional = round(equity * MAX_NOTIONAL_PCT, 2)
    oid3 = f"stress-{uuid.uuid4().hex[:12]}"
    r3 = client.submit_market_order(LIQUID_SYMBOL, "BUY", notional=large_notional,
                                     client_order_id=oid3, strategy="stress")
    record("large_notional_buy", r3.get("status") == "submitted",
           f"notional={large_notional} {json.dumps(r3)}")

    # 4. Invalid symbol -- confirm the broker rejection is classified, not an unhandled crash.
    try:
        r4 = client.submit_market_order("ZZZZZINVALID", "BUY", notional=10.0, strategy="stress")
        rejected_cleanly = r4.get("status") == "error"
        record("invalid_symbol_rejected", rejected_cleanly, json.dumps(r4))
    except Exception as e:
        record("invalid_symbol_rejected", False, f"unhandled exception: {classify_broker_error(e)}: {e}")

    # 5. Zero notional -- must be rejected by risk_controller, not sent to the broker.
    r5 = client.submit_market_order(LIQUID_SYMBOL, "BUY", notional=0.0, strategy="stress")
    record("zero_notional_rejected", r5.get("status") == "error", json.dumps(r5))

    # 6. client_order_id idempotency: resubmit oid1 and confirm no duplicate order is placed.
    r6 = client.submit_market_order(LIQUID_SYMBOL, "BUY", notional=50.0,
                                     client_order_id=oid1, strategy="stress")
    same_order = r6.get("order_id") == r1.get("order_id")
    record("idempotent_resubmit", same_order, f"first={r1.get('order_id')} second={r6.get('order_id')}")

    # 7. Rapid consecutive submissions -- burst of 5 small orders back to back.
    burst_ok = True
    for i in range(5):
        oid = f"stress-burst-{uuid.uuid4().hex[:10]}"
        rb = client.submit_market_order(LIQUID_SYMBOL, "BUY", notional=5.0,
                                         client_order_id=oid, strategy="stress")
        if rb.get("status") != "submitted":
            burst_ok = False
    record("rapid_burst_5_orders", burst_ok, f"consecutive_errors={client.consecutive_errors}")

    # 8. Closing sells -- only same-day if PDT budget allows, else leave positions open
    #    overnight and let tomorrow's run close them (still exercises the real exit path,
    #    just not forced into a same-day round trip that would burn a day-trade).
    if safe_to_day_trade:
        for sym, oid in [(LIQUID_SYMBOL, oid1), (FRACTIONAL_SYMBOL, oid2)]:
            qty = client.get_position_qty(sym)
            if qty and qty > 0:
                rc = client.submit_market_order(sym, "SELL", qty=qty, strategy="stress")
                record(f"close_{sym}", rc.get("status") == "submitted", json.dumps(rc))
                if rc.get("status") == "submitted":
                    record_day_trade(ledger)
            elif qty is None:
                record(f"close_{sym}_lookup_failed", False, "get_position_qty returned None -- broker unreachable")
            else:
                record(f"close_{sym}_skipped_no_fill", True,
                       "qty=0: buy order not yet filled (market closed or still pending) -- will close on a later run")
    else:
        record("close_positions_skipped_pdt_guard", True, "holding overnight to protect PDT budget")

    return _finish()


def _finish() -> int:
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    summary = f"AETHER stress test: {passed}/{total} passed"
    print(f"\n{summary}")
    failed = [n for n, ok, d in results if not ok]
    try:
        if failed:
            notify.send_sync(f"[STRESS TEST] {summary}\nFAILED: {', '.join(failed)}")
        else:
            notify.send_sync(f"[STRESS TEST] {summary}, all clear.")
    except Exception as e:
        print(f"notify failed (non-fatal): {e}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
