#!/usr/bin/env python3
"""US live-trading build: separate LIVE connection, dollar cap, Telegram approval gate.
Crypto is untouched. With default settings (US_LIVE=false, US_REQUIRE_APPROVAL=false) behavior is unchanged.

  python3 patch_us_live.py            # dry run: checks every anchor in both files, changes nothing
  python3 patch_us_live.py --apply    # writes only if ALL anchors match exactly once
"""
import sys
from pathlib import Path

HOME = Path.home()
EXECUTOR = HOME / "alpaca_executor.py"
ENGINE = HOME / "aether_engine.py"

EXECUTOR_EDITS = [
    ("executor: key names",
     '    def __init__(self, paper: bool = True):\n'
     '        self.api_key = os.getenv("APCA_API_KEY_ID")\n'
     '        self.api_secret = os.getenv("APCA_API_SECRET_KEY")\n',
     '    def __init__(self, paper: bool = True, key_env: str = "APCA_API_KEY_ID",\n'
     '                 secret_env: str = "APCA_API_SECRET_KEY"):\n'
     '        self.paper = paper\n'
     '        self.api_key = os.getenv(key_env)\n'
     '        self.api_secret = os.getenv(secret_env)\n'),
    ("executor: honest log label",
     'logger.info(f"Alpaca Trading Client initialized. LIVE_TRADING mode: {self.live_trading}")',
     'logger.info(f"Alpaca Trading Client initialized ({\'PAPER\' if paper else \'LIVE MONEY\'} account). "\n'
     '                            f"LIVE_TRADING flag: {self.live_trading}")'),
]

GATE_CODE = '''

def _us_gate_active() -> bool:
    return US_EXECUTE and (US_LIVE or US_REQUIRE_APPROVAL)


def _us_live_gate(orders, sig, mkey, today, now_et, equity_before) -> bool:
    """Approval gate: always on in LIVE mode, optional in PAPER (US_REQUIRE_APPROVAL=true).
    Returns True only when this exact plan was approved on Telegram today and it is past 15:30 ET.
    Otherwise it creates or keeps a proposal and returns False, so nothing is sent."""
    mode = _us_mode()
    if US_LIVE:
        if us_broker.client is None:
            US_STATUS["note"] = "LIVE blocked: live Alpaca connection unavailable"
            notify.send("[ALERT] US LIVE mode is on but the live Alpaca connection is unavailable "
                        "(check APCA_LIVE_API_KEY_ID / APCA_LIVE_API_SECRET_KEY). No orders sent.",
                        key="us-live-client", cooldown=6 * 3600)
            decision_log.log_changed("us:gate", ("client", today), engine="us", event="live_blocked", mode=mode,
                                     reason="live client unavailable", month=mkey)
            return False
        buys = sum(o.get("notional", 0.0) for o in orders if o["side"] == "BUY")
        if US_LIVE_MAX_USD <= 0 or equity_before > US_LIVE_MAX_USD or buys > US_LIVE_MAX_USD:
            why = f"equity ${equity_before:,.2f}, buys ${buys:,.2f}, cap US_LIVE_MAX_USD=${US_LIVE_MAX_USD:,.2f}"
            US_STATUS["note"] = "LIVE blocked by dollar cap"
            notify.send(f"[ALERT] US LIVE rebalance blocked by the dollar cap ({why}). No orders sent.",
                        key=f"us-live-cap:{today}", cooldown=6 * 3600)
            decision_log.log_changed("us:gate", ("cap", today), engine="us", event="live_blocked", mode=mode,
                                     reason=why, month=mkey)
            return False
    if not orders:
        return True
    key = json.dumps({"month": mkey, "weights": {k: round(v, 4) for k, v in sorted(sig["weights"].items())},
                      "orders": sorted([o["symbol"], o["side"]] for o in orders)}, sort_keys=True)
    after_cutoff = (now_et.hour, now_et.minute) >= US_EXEC_AFTER_ET
    with STATE_LOCK:
        state = load_state()
        p = state["us"].get("live_proposal")
        if p and p.get("key") == key and p.get("date") == today:
            if p["status"] == "PENDING":
                US_STATUS["note"] = f"awaiting Telegram approval of proposal {p['id']}"
                return False
            if p["status"] == "REJECTED":
                US_STATUS["note"] = f"proposal {p['id']} rejected; next attempt on the next trading day"
                return False
            if p["status"] == "APPROVED":
                if not after_cutoff:
                    US_STATUS["note"] = f"proposal {p['id']} approved; orders go out after 15:30 ET"
                    return False
                p["status"] = "USED"
                p["used_at"] = utcnow_str()
                save_state(state)
                decision_log.log("us", "approval_used", mode=mode, decision="execute",
                                 reason=f"proposal {p['id']}", month=mkey)
                return True
        if p and p.get("status") in ("PENDING", "APPROVED"):
            p["status"] = "EXPIRED"
            decision_log.log("us", "proposal_expired", mode=mode,
                             reason=f"proposal {p['id']} replaced (plan or day changed)", month=mkey)
        pid = os.urandom(3).hex()
        state["us"]["live_proposal"] = {"id": pid, "key": key, "date": today, "month": mkey, "status": "PENDING",
                                        "created_at": utcnow_str(), "orders": orders, "weights": sig["weights"]}
        save_state(state)
    lines = [f"[US {mode}] Rebalance proposal {pid} for {mkey}",
             "Targets: " + ", ".join(f"{k} {v * 100:.0f}%" for k, v in sorted(sig["weights"].items())),
             f"Equity about ${equity_before:,.2f}"]
    for o in orders:
        amt = f"${o['notional']:,.2f}" if o["side"] == "BUY" else f"{o['qty']:.4f} sh"
        lines.append(f"  {o['side']} {o['symbol']} {amt}")
    lines.append(f"Approve: /approve_{pid}    Reject: /reject_{pid}")
    lines.append("Valid today only. If approved, orders go out after 15:30 ET.")
    notify.send("\\n".join(lines))
    decision_log.log("us", "proposal", mode=mode, decision="PENDING", reason=f"proposal {pid}", month=mkey,
                     orders=orders, weights=sig["weights"])
    US_STATUS["note"] = f"awaiting Telegram approval of proposal {pid}"
    return False


def _us_live_decide(cmd: str) -> str:
    """Telegram /approve_<id> or /reject_<id>. Only the configured chat can reach this (see notify.py)."""
    action, _, pid = cmd.lstrip("/").partition("_")
    today = datetime.now(timezone.utc).astimezone(NY).strftime("%Y-%m-%d")
    with STATE_LOCK:
        state = load_state()
        p = state["us"].get("live_proposal")
        if not p or p.get("id") != pid:
            return "No matching US proposal. It may have expired or been replaced; check the latest message."
        if p["status"] != "PENDING":
            return f"Proposal {pid} is already {p['status']}; nothing changed."
        if p["date"] != today:
            p["status"] = "EXPIRED"
            save_state(state)
            return f"Proposal {pid} was for {p['date']} and has expired. A new one will be sent."
        p["status"] = "APPROVED" if action == "approve" else "REJECTED"
        p["decided_at"] = utcnow_str()
        save_state(state)
        status, month = p["status"], p["month"]
    decision_log.log("us", "approval", mode=_us_mode(), decision=status, reason=f"proposal {pid}", month=month)
    if status == "APPROVED":
        return f"US proposal {pid} APPROVED. Orders go out after 15:30 ET if the plan is unchanged."
    return f"US proposal {pid} REJECTED. No US orders today."
'''

ENGINE_EDITS = [
    ("settings + live connection",
     "broker = AlpacaExecutionClient(paper=True)\n",
     "broker = AlpacaExecutionClient(paper=True)\n"
     "# US engine only: a separate LIVE-money connection, used only when US_LIVE=true AND US_EXECUTE=true.\n"
     "US_LIVE = os.getenv(\"US_LIVE\", \"false\").lower() == \"true\"\n"
     "US_LIVE_MAX_USD = float(os.getenv(\"US_LIVE_MAX_USD\", \"0\"))\n"
     "US_REQUIRE_APPROVAL = US_LIVE or os.getenv(\"US_REQUIRE_APPROVAL\", \"false\").lower() == \"true\"\n"
     "us_broker = (AlpacaExecutionClient(paper=False, key_env=\"APCA_LIVE_API_KEY_ID\",\n"
     "                                   secret_env=\"APCA_LIVE_API_SECRET_KEY\") if US_LIVE else broker)\n"),
    ("mode label",
     '    return "PAPER" if US_EXECUTE else "SHADOW"\n',
     '    return ("LIVE" if US_LIVE else "PAPER") if US_EXECUTE else "SHADOW"\n'),
    ("holdings -> us_broker",
     "        pos = broker.get_positions_map()\n        if pos is None:\n",
     "        pos = us_broker.get_positions_map()\n        if pos is None:\n"),
    ("sell -> us_broker",
     '            r = broker.submit_market_order(sym, "SELL", qty=o["qty"])\n',
     '            r = us_broker.submit_market_order(sym, "SELL", qty=o["qty"])\n'),
    ("buy -> us_broker",
     '            r = broker.submit_market_order(sym, "BUY", notional=o["notional"])\n',
     '            r = us_broker.submit_market_order(sym, "BUY", notional=o["notional"])\n'),
    ("fill -> us_broker",
     '        f = broker.wait_for_fill(r["order_id"])\n',
     '        f = us_broker.wait_for_fill(r["order_id"])\n'),
    ("positions after -> us_broker",
     "    pos = broker.get_positions_map() or {}\n",
     "    pos = us_broker.get_positions_map() or {}\n"),
    ("open orders -> us_broker",
     "        open_syms = broker.open_order_symbols()\n",
     "        open_syms = us_broker.open_order_symbols()\n"),
    ("approval gate in rebalance",
     '    orders = us_strategy.plan_orders(holdings, prices, sig["weights"], equity_before)\n'
     '    if US_EXECUTE:\n',
     '    orders = us_strategy.plan_orders(holdings, prices, sig["weights"], equity_before)\n'
     '    if _us_gate_active() and not _us_live_gate(orders, sig, mkey, today, now_et, equity_before):\n'
     '        return\n'
     '    if US_EXECUTE:\n'),
    ("propose before 15:30 when gated",
     "        if (now_et.hour, now_et.minute) < US_EXEC_AFTER_ET:\n",
     "        if (now_et.hour, now_et.minute) < US_EXEC_AFTER_ET and not _us_gate_active():\n"),
    ("gate functions",
     "def _us_snapshot(fetch_prices, today: str):\n",
     GATE_CODE.lstrip("\n") + "\n\ndef _us_snapshot(fetch_prices, today: str):\n"),
    ("telegram commands",
     "def handle_command(cmd: str):\n",
     "def handle_command(cmd: str):\n"
     "    if cmd.startswith(\"/approve_\") or cmd.startswith(\"/reject_\"):\n"
     "        return _us_live_decide(cmd)\n"),
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
    if "US_REQUIRE_APPROVAL" in en or "key_env" in ex:
        sys.exit("Live build already applied; nothing to do.")
    ok = check(EXECUTOR, ex, EXECUTOR_EDITS) & check(ENGINE, en, ENGINE_EDITS)
    if not ok:
        sys.exit("Not applied: every anchor must match exactly once.")
    if not apply:
        print("Dry run passed. Re-run with --apply to write the changes.")
        return
    for _, old, new in EXECUTOR_EDITS:
        ex = ex.replace(old, new, 1)
    for _, old, new in ENGINE_EDITS:
        en = en.replace(old, new, 1)
    EXECUTOR.write_text(ex)
    ENGINE.write_text(en)
    print(f"Applied {len(EXECUTOR_EDITS)} edits to {EXECUTOR.name} and {len(ENGINE_EDITS)} edits to {ENGINE.name}.")


if __name__ == "__main__":
    main()
