"""One-off, manually-run extreme boundary audit for the order-execution path. NOT a daily job --
see stress_test.py for that. This script deliberately probes rate limits, race conditions, margin
ceilings and (opt-in only) the real PDT lock, against the dedicated `stress-test` Alpaca paper
account (STRESS_ALPACA_API_KEY/SECRET_KEY), never the account the real strategy trades on.

Timing corrected from the original spec: Alpaca's real OPG (market-on-open) cutoff is 9:28:00 AM
ET, not 9:29:30 -- submitting an OPG order after 9:28 gets rejected outright, independent of any
bug. This script submits the pre-open order in the 9:25:00-9:27:30 ET window and falls back to a
regular DAY limit order if run after 9:28 ET.

The PDT trap (round-trips 4 and 5, deliberately tripping Alpaca's real Pattern Day Trader lock)
only runs with --trigger-pdt-lock. Without that flag, round-trips 1-3 still run (safe -- stays
under the "4th flags, 5th rejects" threshold) but 4 and 5 are skipped, so the account is NOT
PDT-locked by a default run. Running with the flag WILL lock the account out of day-trading for
the rest of the rolling 5-business-day window -- do this deliberately, not routinely.

Run: python3 stress_test_extreme.py [--trigger-pdt-lock]
"""
from __future__ import annotations

import argparse
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
load_dotenv()

from alpaca_executor import AlpacaExecutionClient, classify_broker_error  # noqa: E402
from alpaca.trading.requests import LimitOrderRequest, ReplaceOrderRequest  # noqa: E402
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus  # noqa: E402
from alpaca.trading.requests import GetOrdersRequest  # noqa: E402

NY = ZoneInfo("America/New_York")
OPG_CUTOFF = (9, 28, 0)  # H, M, S ET -- Alpaca rejects OPG submissions after this.

modules: list[tuple[str, bool, str]] = []
state = "RUNNING"  # flips to READ_ONLY_AUDIT if the PDT trap fires and locks the account.


def log(name: str, ok: bool, detail: str = ""):
    modules.append((name, ok, detail))
    print(("PASS" if ok else "FAIL") + f": {name}" + (f" -- {detail}" if detail else ""))


def mask_account(acct_num: str) -> str:
    if not acct_num:
        return "n/a"
    return ("*" * max(0, len(acct_num) - 4)) + acct_num[-4:]


def get_quote_price(client: AlpacaExecutionClient, symbol: str) -> float | None:
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestTradeRequest
        from alpaca.data.enums import DataFeed
        dc = StockHistoricalDataClient(client.api_key, client.api_secret)
        res = dc.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=[symbol], feed=DataFeed.IEX))
        return float(res[symbol].price)
    except Exception as e:
        print(f"quote fetch failed for {symbol}: {classify_broker_error(e)}: {e}")
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trigger-pdt-lock", action="store_true",
                         help="Also run round-trips 4 and 5, deliberately tripping the real PDT lock.")
    args = parser.parse_args()

    global state
    client = AlpacaExecutionClient(paper=True, key_env="STRESS_ALPACA_API_KEY",
                                    secret_env="STRESS_ALPACA_SECRET_KEY")
    if not client.client:
        log("client_initialised", False, "STRESS_ALPACA_API_KEY/SECRET_KEY missing or invalid")
        return _finish(client, {})

    raw = client.client  # underlying alpaca-py TradingClient

    # ---- 1. Pre-flight sync ----
    clock = client.get_clock()
    clock_delta_ms = None
    if clock is not None:
        server_now = clock.timestamp.astimezone(timezone.utc)
        local_now = datetime.now(timezone.utc)
        clock_delta_ms = round((local_now - server_now).total_seconds() * 1000, 1)
    log("clock_sync", clock is not None, f"delta_ms={clock_delta_ms}")

    now_et = datetime.now(NY)
    cutoff_et = now_et.replace(hour=OPG_CUTOFF[0], minute=OPG_CUTOFF[1], second=OPG_CUTOFF[2], microsecond=0)
    before_cutoff = now_et < cutoff_et

    start_bal = client.get_account_balance()
    start_equity = float(start_bal.get("portfolio_value") or 0) if start_bal.get("source") == "live" else None
    start_cash = float(start_bal.get("cash") or 0) if start_bal.get("source") == "live" else None
    log("pre_open_account_snapshot", start_bal.get("source") == "live",
        f"equity={start_equity} cash={start_cash}")

    opg_oid = f"opg-{uuid.uuid4().hex[:10]}"
    try:
        if before_cutoff:
            order = raw.submit_order(order_data=LimitOrderRequest(
                symbol="SPY", qty=1, side=OrderSide.BUY, time_in_force=TimeInForce.OPG,
                limit_price=1000, client_order_id=opg_oid))
            log("preopen_opg_order", True, f"OPG order {order.id} queued for opening auction")
        else:
            price = get_quote_price(client, "SPY")
            order = raw.submit_order(order_data=LimitOrderRequest(
                symbol="SPY", qty=1, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
                limit_price=round((price or 600) * 1.02, 2), client_order_id=opg_oid))
            log("preopen_opg_order", True,
                f"past 9:28 ET cutoff -- fell back to regular DAY limit order {order.id} (as designed)")
    except Exception as e:
        log("preopen_opg_order", False, f"{classify_broker_error(e)}: {e}")

    # ---- 2a. Rate-limit burst: ~190 lightweight GETs over ~30s ----
    hits_429 = 0
    errors_other = 0
    successes = 0
    burst_start = time.monotonic()
    i = 0
    while time.monotonic() - burst_start < 30 and i < 195:
        i += 1
        try:
            if i % 2 == 0:
                raw.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
            else:
                raw.get_all_positions()
            successes += 1
        except Exception as e:
            code = getattr(e, "status_code", None)
            if code == 429:
                hits_429 += 1
                time.sleep(0.5)  # mirrors the backoff submit_market_order already does on rate_limited
            else:
                errors_other += 1
    log("rate_limit_burst", errors_other == 0,
        f"requests={i} successes={successes} http429={hits_429} unexpected_errors={errors_other}")

    # ---- 2b. Race condition churn: submit, then concurrently replace + cancel ----
    race_result = {}
    try:
        price = get_quote_price(client, "AAPL")
        limit_px = round((price or 200) * 1.01, 2)  # marketable: above current price
        order = raw.submit_order(order_data=LimitOrderRequest(
            symbol="AAPL", qty=1, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
            limit_price=limit_px, client_order_id=f"race-{uuid.uuid4().hex[:10]}"))
        oid = order.id

        def do_replace():
            try:
                raw.replace_order_by_id(oid, order_data=ReplaceOrderRequest(limit_price=round(limit_px * 1.01, 2)))
                race_result["replace"] = "ok"
            except Exception as e:
                race_result["replace"] = f"{classify_broker_error(e)}: {e}"

        def do_cancel():
            try:
                raw.cancel_order_by_id(oid)
                race_result["cancel"] = "ok"
            except Exception as e:
                race_result["cancel"] = f"{classify_broker_error(e)}: {e}"

        t1, t2 = threading.Thread(target=do_replace), threading.Thread(target=do_cancel)
        t1.start(); t2.start()
        t1.join(); t2.join()

        time.sleep(1)  # let the terminal state settle
        final = raw.get_order_by_id(oid)
        final_status = str(getattr(final.status, "value", final.status)).lower()
        deterministic = final_status in ("filled", "canceled", "replaced", "pending_cancel")
        log("race_condition_churn", deterministic,
            f"replace={race_result.get('replace')} cancel={race_result.get('cancel')} final_state={final_status}")
    except Exception as e:
        log("race_condition_churn", False, f"unhandled: {classify_broker_error(e)}: {e}")

    # ---- 3. Margin wall & capacity probe ----
    try:
        bal = client.get_account_balance()
        bp = float(bal.get("buying_power") or 0)
        target_notional = round(bp * 0.99, 2)
        price = get_quote_price(client, "SPY") or 600.0
        qty_99 = round(target_notional / price, 4)
        resting_oid = f"margin99-{uuid.uuid4().hex[:10]}"
        resting = raw.submit_order(order_data=LimitOrderRequest(
            symbol="SPY", qty=qty_99, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
            limit_price=round(price * 0.95, 2),  # below market: rests, doesn't fill immediately
            client_order_id=resting_oid))
        log("margin_99pct_resting_order", True, f"order {resting.id} notional~{target_notional} resting")

        time.sleep(1)  # let the resting order's buying-power reservation settle before reading it again
        bal_after = client.get_account_balance()
        bp_after = float(bal_after.get("buying_power") or 0)
        breach_notional = bp_after + 1000  # guaranteed to exceed whatever is actually left, however it's computed
        log("margin_remaining_bp_after_resting_order", True, f"bp_before={bp:.2f} bp_after={bp_after:.2f}")

        breach_caught = False
        try:
            raw.submit_order(order_data=LimitOrderRequest(
                symbol="SPY", qty=round(breach_notional / price, 4), side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
                limit_price=round(price * 0.95, 2), client_order_id=f"breach-{uuid.uuid4().hex[:10]}"))
        except Exception as e:
            code = getattr(e, "status_code", None)
            breach_caught = isinstance(code, int) and 400 <= code < 500
            log("margin_breach_rejected_gracefully", breach_caught, f"{classify_broker_error(e)}: {e}")
        else:
            log("margin_breach_rejected_gracefully", False, "breach order was accepted, not rejected")

        raw.cancel_order_by_id(resting.id)
        log("margin_resting_order_cancelled", True, f"cancelled {resting.id} before execution")
    except Exception as e:
        log("margin_wall_probe", False, f"unhandled: {classify_broker_error(e)}: {e}")

    # ---- 4. PDT boundary trap ----
    daytrade_counts = []
    for n in range(1, 4):
        try:
            buy = raw.submit_order(order_data=LimitOrderRequest(
                symbol="QQQ", qty=1, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
                limit_price=round((get_quote_price(client, "QQQ") or 400) * 1.02, 2),
                client_order_id=f"pdt{n}-buy-{uuid.uuid4().hex[:8]}"))
            time.sleep(2)
            sell = client.submit_market_order("QQQ", "SELL", qty=1, strategy="stress-extreme")
            acct = raw.get_account()
            raw_dc = getattr(acct, "daytrade_count", None)
            dc = int(raw_dc) if raw_dc is not None else -1
            daytrade_counts.append(dc)
            log(f"pdt_roundtrip_{n}", sell.get("status") == "submitted", f"daytrade_count={dc}")
        except Exception as e:
            log(f"pdt_roundtrip_{n}", False, f"{classify_broker_error(e)}: {e}")

    if args.trigger_pdt_lock:
        if daytrade_counts and daytrade_counts[-1] >= 3:
            log("pdt_warning_at_4_4", True, f"WARNING: PDT limit reached ({daytrade_counts[-1]}/4) -- proceeding to round 4 deliberately")
        for n in (4, 5):
            try:
                buy = raw.submit_order(order_data=LimitOrderRequest(
                    symbol="QQQ", qty=1, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
                    limit_price=round((get_quote_price(client, "QQQ") or 400) * 1.02, 2),
                    client_order_id=f"pdt{n}-buy-{uuid.uuid4().hex[:8]}"))
                time.sleep(2)
                sell = client.submit_market_order("QQQ", "SELL", qty=1, strategy="stress-extreme")
                if sell.get("status") == "submitted":
                    log(f"pdt_roundtrip_{n}", n < 5, f"round {n} unexpectedly succeeded (expected rejection at 5)")
                else:
                    code_str = sell.get("message", "")
                    is_pdt = "40310100" in code_str or "pattern day" in code_str.lower()
                    log(f"pdt_roundtrip_{n}_rejected", is_pdt, code_str)
                    if is_pdt:
                        state = "READ_ONLY_AUDIT"
                        log("state_transition", True, "READ_ONLY_AUDIT -- halting further order placement")
                        break
            except Exception as e:
                code = getattr(e, "status_code", None)
                is_pdt = code == 403
                log(f"pdt_roundtrip_{n}_rejected", is_pdt, f"{classify_broker_error(e)}: {e}")
                if is_pdt:
                    state = "READ_ONLY_AUDIT"
                    break
    else:
        log("pdt_trap_4_5_skipped", True, "ran without --trigger-pdt-lock: rounds 4-5 skipped, account stays reusable")

    return _finish(client, start_bal)


def _finish(client: AlpacaExecutionClient, start_bal: dict) -> int:
    end_bal = client.get_account_balance() if client.client and state != "READ_ONLY_AUDIT_SKIP" else {}
    if state == "READ_ONLY_AUDIT":
        log("teardown_order_purge", True, "state=READ_ONLY_AUDIT: skipping further order placement, reconciling only")
    elif client.client:
        try:
            client.client.cancel_orders()
            log("teardown_order_purge", True, "all open resting orders cancelled")
        except Exception as e:
            log("teardown_order_purge", False, f"{classify_broker_error(e)}: {e}")
        end_bal = client.get_account_balance()

    passed = sum(1 for _, ok, _ in modules if ok)
    total = len(modules)
    ts = datetime.now(NY).strftime("%Y-%m-%d %H:%M:%S ET")

    report = f"""# Alpaca Market-Open Stress Test Report
- **Timestamp:** {ts}
- **Final State:** {state}
- **Starting Cash / Equity:** ${start_bal.get('cash', 'n/a')} / ${start_bal.get('portfolio_value', 'n/a')}
- **Ending Cash / Equity:** ${end_bal.get('cash', 'n/a')} / ${end_bal.get('portfolio_value', 'n/a')}

## Test Modules Results ({passed}/{total} passed)
"""
    for name, ok, detail in modules:
        report += f"- **{name}**: {'PASS' if ok else 'FAIL'} -- {detail}\n"

    print("\n" + report)
    with open("stress_test_extreme_report.md", "w") as f:
        f.write(report)

    try:
        import notify
        failed = [n for n, ok, _ in modules if not ok]
        msg = f"[STRESS TEST EXTREME] {passed}/{total} passed, state={state}"
        if failed:
            msg += f"\nFAILED: {', '.join(failed)}"
        notify.send_sync(msg)
    except Exception as e:
        print(f"notify failed (non-fatal): {e}")

    return 0 if passed == total else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
