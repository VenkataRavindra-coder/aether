#!/usr/bin/env python3
"""Fixes from the 26 Sep audit (applies on top of the live build, commit efcfcd4).

 1 approval identity: mode + account in the key; orders may not exceed approved sizes (+2%)
 2 cap: must be a finite positive number; LIVE also checks the broker's whole-account equity
 3 unreadable positions after fills: rebalance stays incomplete, confirmed fills are booked
 4 timeout: cancel the rest, re-read final fill before booking cash; unresolved -> engine freezes
 5 broker clock unavailable: market treated as closed (no trading on a guessed calendar)
 6 catch-up rebalances are flagged in the proposal and the decision log

  python3 patch_us_fixes.py            # dry run
  python3 patch_us_fixes.py --apply    # validates and compiles both files in memory, then writes both
"""
import os
import sys
from pathlib import Path

HOME = Path.home()
EXECUTOR = HOME / "alpaca_executor.py"
ENGINE = HOME / "aether_engine.py"

EXECUTOR_EDITS = [
    ("executor: cancel_order + account_id",
     "    def wait_for_fill(self, order_id: str, timeout: float = 90.0, poll: float = 2.0):\n",
     "    def cancel_order(self, order_id: str) -> bool:\n"
     "        \"\"\"Cancels an open order. False if it could not be cancelled (e.g. it already filled).\"\"\"\n"
     "        if not self.client:\n"
     "            return False\n"
     "        try:\n"
     "            self.client.cancel_order_by_id(order_id)\n"
     "            return True\n"
     "        except Exception as e:\n"
     "            logger.warning(f\"Cancel failed for {order_id}: {str(e)}\")\n"
     "            return False\n"
     "\n"
     "    def account_id(self):\n"
     "        \"\"\"Broker account number (cached). None if unavailable.\"\"\"\n"
     "        if getattr(self, \"_account_id\", None):\n"
     "            return self._account_id\n"
     "        if not self.client:\n"
     "            return None\n"
     "        try:\n"
     "            self._account_id = str(self.client.get_account().account_number)\n"
     "            return self._account_id\n"
     "        except Exception as e:\n"
     "            logger.warning(f\"Could not read account id: {str(e)}\")\n"
     "            return None\n"
     "\n"
     "    def wait_for_fill(self, order_id: str, timeout: float = 90.0, poll: float = 2.0):\n"),
]

OLD_EXEC = '''def _us_execute_paper(orders, prices, cash):
    results = []
    for o in orders:
        sym = o["symbol"]
        if o["side"] == "SELL":
            r = us_broker.submit_market_order(sym, "SELL", qty=o["qty"])
        else:
            r = us_broker.submit_market_order(sym, "BUY", notional=o["notional"])
        if r.get("status") != "submitted":
            results.append({**o, "state": "error", "detail": r.get("message")})
            continue
        f = us_broker.wait_for_fill(r["order_id"])
        px = f["avg_price"] or prices[sym]
        value = f["filled_qty"] * px
        cash += value if o["side"] == "SELL" else -value
        results.append({**o, "state": f["state"], "filled_qty": f["filled_qty"], "avg_price": px, "order_id": r["order_id"]})
    pos = us_broker.get_positions_map() or {}
    return results, {s: q for s, q in pos.items() if s in us_strategy.UNIVERSE and q > 1e-9}, cash
'''

NEW_EXEC = '''_US_FINAL_STATES = {"filled", "canceled", "expired", "rejected", "done_for_day"}


def _us_execute_paper(orders, prices, cash, holdings_before=None):
    """Sends orders and books only broker-confirmed fills. An order still open at the poll timeout is
    cancelled and re-read, so a late fill cannot leave cash stale. An order that still cannot be
    reconciled is marked 'unreconciled' (the engine then freezes). If positions cannot be read after
    the fills, the confirmed fills are booked onto the known holdings and the rebalance stays incomplete."""
    results = []
    book = dict(holdings_before or {})
    for o in orders:
        sym = o["symbol"]
        if o["side"] == "SELL":
            r = us_broker.submit_market_order(sym, "SELL", qty=o["qty"])
        else:
            r = us_broker.submit_market_order(sym, "BUY", notional=o["notional"])
        if r.get("status") != "submitted":
            results.append({**o, "state": "error", "detail": r.get("message")})
            continue
        f = us_broker.wait_for_fill(r["order_id"])
        if f["state"] not in _US_FINAL_STATES:
            us_broker.cancel_order(r["order_id"])
            f = us_broker.wait_for_fill(r["order_id"], timeout=60.0)
        qty = float(f.get("filled_qty") or 0.0)
        px = f["avg_price"] or prices[sym]
        cash += qty * px if o["side"] == "SELL" else -qty * px
        book[sym] = book.get(sym, 0.0) + (qty if o["side"] == "BUY" else -qty)
        state = f["state"] if f["state"] in _US_FINAL_STATES else "unreconciled"
        results.append({**o, "state": state, "filled_qty": qty, "avg_price": px, "order_id": r["order_id"]})
    book = {s: q for s, q in book.items() if q > 1e-9}
    pos = us_broker.get_positions_map()
    if pos is None:
        results.append({"symbol": "*", "side": "CHECK", "qty": 0.0, "notional": 0.0, "state": "positions_unreadable"})
        return results, book, cash
    return results, {s: q for s, q in pos.items() if s in us_strategy.UNIVERSE and q > 1e-9}, cash
'''

WITHIN = '''US_APPROVAL_TOLERANCE = 0.02


def _us_within_approval(orders, approved) -> bool:
    """True if every order matches an approved order (same symbol and side) and is no larger than approved
    (+2% for price moves), and total buying is within the approved total. Smaller orders are fine."""
    ok = {(a["symbol"], a["side"]): a for a in approved}
    tol = 1 + US_APPROVAL_TOLERANCE
    for o in orders:
        a = ok.get((o["symbol"], o["side"]))
        if a is None:
            return False
        if o["side"] == "BUY" and o.get("notional", 0.0) > a.get("notional", 0.0) * tol + 0.01:
            return False
        if o["side"] == "SELL" and o.get("qty", 0.0) > a.get("qty", 0.0) * tol + 1e-6:
            return False
    buys = sum(o.get("notional", 0.0) for o in orders if o["side"] == "BUY")
    return buys <= sum(a.get("notional", 0.0) for a in approved if a["side"] == "BUY") * tol + 0.01


def _us_live_decide(cmd: str) -> str:
'''

ENGINE_EDITS = [
    ("clock: docstring",
     '    """(is_open, now_et). Alpaca\'s clock knows holidays; the fallback is a plain weekday/hours check."""\n',
     '    """(is_open, now_et). Alpaca\'s clock knows holidays; without it the market is treated as closed."""\n'),
    ("clock: no guessed calendar",
     "    now = datetime.now(timezone.utc).astimezone(NY)\n"
     "    return (now.weekday() < 5 and (9, 30) <= (now.hour, now.minute) < (16, 0)), now\n",
     "    now = datetime.now(timezone.utc).astimezone(NY)\n"
     "    logger.warning(\"Broker clock unavailable: treating the US market as closed (no trading on a guessed calendar).\")\n"
     "    return False, now\n"),
    ("execution: book confirmed fills only", OLD_EXEC, NEW_EXEC),
    ("execution: pass known holdings",
     "        results, holdings, cash = _us_execute_paper(orders, prices, cash)\n",
     "        results, holdings, cash = _us_execute_paper(orders, prices, cash, holdings)\n"),
    ("freeze on unreconciled orders",
     "        if not failed:\n"
     "            u[\"last_rebalance_month\"] = mkey\n"
     "            u[\"last_rebalance_at\"] = utcnow_str()\n",
     "        if not failed:\n"
     "            u[\"last_rebalance_month\"] = mkey\n"
     "            u[\"last_rebalance_at\"] = utcnow_str()\n"
     "        stuck = [r.get(\"order_id\") for r in results if r[\"state\"] == \"unreconciled\"]\n"
     "        if stuck:\n"
     "            u[\"frozen\"] = f\"unreconciled order(s) {stuck}: check the broker, then clear us.frozen\"\n"),
    ("frozen engine does not trade",
     "def _us_rebalance(us: dict, today: str, mkey: str, now_et: datetime, fetch_daily, fetch_prices):\n",
     "def _us_rebalance(us: dict, today: str, mkey: str, now_et: datetime, fetch_daily, fetch_prices):\n"
     "    if us.get(\"frozen\"):\n"
     "        US_STATUS[\"note\"] = f\"US engine frozen: {us['frozen']}\"\n"
     "        notify.send(f\"[ALERT] US ETF engine is frozen and will not trade: {us['frozen']}\",\n"
     "                    key=\"us-frozen\", cooldown=6 * 3600)\n"
     "        return\n"),
    ("cap: finite, positive, whole account",
     "        if US_LIVE_MAX_USD <= 0 or equity_before > US_LIVE_MAX_USD or buys > US_LIVE_MAX_USD:\n"
     "            why = f\"equity ${equity_before:,.2f}, buys ${buys:,.2f}, cap US_LIVE_MAX_USD=${US_LIVE_MAX_USD:,.2f}\"\n",
     "        bal = us_broker.get_account_balance() or {}\n"
     "        try:\n"
     "            acct_eq = float(bal.get(\"portfolio_value\")) if bal.get(\"source\") == \"live\" else float(\"nan\")\n"
     "        except (TypeError, ValueError):\n"
     "            acct_eq = float(\"nan\")\n"
     "        cap = US_LIVE_MAX_USD\n"
     "        if not (0 < cap < float(\"inf\") and equity_before <= cap and buys <= cap and acct_eq <= cap):\n"
     "            why = (f\"strategy equity ${equity_before:,.2f}, buys ${buys:,.2f}, account equity ${acct_eq:,.2f}, \"\n"
     "                   f\"cap US_LIVE_MAX_USD=${cap:,.2f} (must be a positive number at least as large as all three)\")\n"),
    ("approval key: mode + account",
     "    key = json.dumps({\"month\": mkey, \"weights\": {k: round(v, 4) for k, v in sorted(sig[\"weights\"].items())},\n"
     "                      \"orders\": sorted([o[\"symbol\"], o[\"side\"]] for o in orders)}, sort_keys=True)\n",
     "    account = us_broker.account_id()\n"
     "    if US_LIVE and not account:\n"
     "        US_STATUS[\"note\"] = \"LIVE blocked: live account id unreadable\"\n"
     "        decision_log.log_changed(\"us:gate\", (\"account\", today), engine=\"us\", event=\"live_blocked\", mode=mode,\n"
     "                                 reason=\"live account id unreadable\", month=mkey)\n"
     "        return False\n"
     "    key = json.dumps({\"month\": mkey, \"mode\": mode, \"account\": account or \"unknown\",\n"
     "                      \"weights\": {k: round(v, 4) for k, v in sorted(sig[\"weights\"].items())},\n"
     "                      \"orders\": sorted([o[\"symbol\"], o[\"side\"]] for o in orders)}, sort_keys=True)\n"),
    ("approval: sizes within approved",
     "            if p[\"status\"] == \"APPROVED\":\n",
     "            if p[\"status\"] == \"APPROVED\" and _us_within_approval(orders, p.get(\"orders\") or []):\n"),
    ("approval: size check function", "def _us_live_decide(cmd: str) -> str:\n", WITHIN),
    ("catch-up flag in proposal",
     "    lines.append(\"Valid today only. If approved, orders go out after 15:30 ET.\")\n",
     "    if now_et.day > 4:\n"
     "        lines.append(\"Note: catch-up rebalance (later than the month's first trading days).\")\n"
     "    lines.append(\"Valid today only. If approved, orders go out after 15:30 ET.\")\n"),
    ("catch-up flag in decision log",
     "                     orders=orders, weights=sig[\"weights\"])\n",
     "                     orders=orders, weights=sig[\"weights\"], catch_up=now_et.day > 4)\n"),
]


def check(path, src, edits):
    ok = True
    for name, old, _ in edits:
        n = src.count(old)
        print(f"{'OK ' if n == 1 else 'BAD'} {path.name}: {name} (anchor found {n}x)")
        ok &= n == 1
    return ok


def main():
    apply = "--apply" in sys.argv
    ex, en = EXECUTOR.read_text(), ENGINE.read_text()
    if "_us_within_approval" in en or "def cancel_order" in ex:
        sys.exit("Fixes already applied; nothing to do.")
    if "US_REQUIRE_APPROVAL" not in en:
        sys.exit("The live build (efcfcd4) is not installed; apply it first.")
    ok = check(EXECUTOR, ex, EXECUTOR_EDITS) & check(ENGINE, en, ENGINE_EDITS)
    if not ok:
        sys.exit("Not applied: every anchor must match exactly once.")
    new_ex, new_en = ex, en
    for _, old, new in EXECUTOR_EDITS:
        new_ex = new_ex.replace(old, new, 1)
    for _, old, new in ENGINE_EDITS:
        new_en = new_en.replace(old, new, 1)
    compile(new_ex, str(EXECUTOR), "exec")
    compile(new_en, str(ENGINE), "exec")
    if not apply:
        print("Dry run passed (both patched files compile in memory). Re-run with --apply to write.")
        return
    tmp_ex, tmp_en = EXECUTOR.with_suffix(".py.new"), ENGINE.with_suffix(".py.new")
    tmp_ex.write_text(new_ex)
    tmp_en.write_text(new_en)
    os.replace(tmp_ex, EXECUTOR)
    try:
        os.replace(tmp_en, ENGINE)
    except Exception:
        EXECUTOR.write_text(ex)
        raise
    print(f"Applied {len(EXECUTOR_EDITS)} edit(s) to {EXECUTOR.name} and {len(ENGINE_EDITS)} to {ENGINE.name}.")


if __name__ == "__main__":
    main()
