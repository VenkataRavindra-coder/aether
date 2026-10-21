"""Automated canary round trips (strategy.md 10, amendment 2026-10-21: "Automated round trips").

Owner decision 2026-10-21: the repeated buy-then-sell round trips of canary steps 1 and 3 run on a
schedule instead of by hand. Everything else about the canary is unchanged: every order still goes
through canary.py's own `buy` / `sell` commands, so the same preflight, pre-registered buy window,
1% price band, spread line, risk_controller caps (canary key: $15/order, $30/day), live ledger and
halt file apply. Steps 2, 4, 5 and 6 (restarts, halt, cancel/replace, fractional check) stay manual.

  python3 canary_auto.py run     # timer, Mon-Fri 10:05 New York: one same-day round trip (buy, then sell)
  python3 canary_auto.py close   # timer, Mon-Fri 15:52 New York: cancel leftovers, record end-of-day cash
  python3 canary_auto.py plan    # read-only: what `run` would do now, and why

Fails closed. It does nothing unless CANARY_AUTO=1, and it stops (Telegram alert, non-zero exit so
the job shows as failing) on any measurement failure, protocol violation, unknown position or
leftover open order. It buys at most once per run, and it stops buying once the pre-registered
10 round trips are done. Same-day round trips are allowed: FINRA retired the PDT rule and Alpaca
moved to intraday margin on 2026-06-04.
No local state: every run re-reads the broker, like canary.py.
"""
from __future__ import annotations

import io
import math
import os
import re
import sys
import time
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

_HERE = Path(__file__).resolve().parent

def _load_env_file(path: Path) -> None:
    """Same loader as canary.py. It must run BEFORE importing canary: canary imports risk_controller
    and safety_guard, which read their paths (halt file, risk state, ledger) from the environment."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


if __name__ == "__main__":
    _load_env_file(_HERE / ".env")
    _load_env_file(_HERE / ".env.live")

import canary  # noqa: E402
import safety_guard  # noqa: E402

BUY_USD = 8.0                 # strategy.md 10 step 1: `buy --usd 8`
FILL_WAIT_S = canary.PREREG["max_fill_seconds"]   # unfilled after this -> cancel (the leg would FAIL anyway)
POLL_S = 15
BUY_RETRIES = 8               # only for "spread too wide" / "no usable quote" refusals
BUY_RETRY_GAP_S = 300


def enabled() -> bool:
    return os.getenv("CANARY_AUTO", "0").strip().lower() in ("1", "true", "yes", "on")


def _notify(text: str) -> None:
    try:
        import notify
        notify.send_sync("🐤 Canary auto: " + text)
    except Exception:
        pass


def _log(event, **kw):
    try:
        import decision_log
        decision_log.log("canary_auto", event, mode="live", **kw)
    except Exception:
        pass


def _et_day(ts) -> str | None:
    t = canary._ts(ts)
    return t.astimezone(safety_guard.ET).date().isoformat() if t else None


def last_buy_fill_day(orders_all) -> str | None:
    """ET date of the newest filled canary BUY (the leg the current position came from)."""
    buys = [o for o in orders_all if canary.is_canary(o.client_order_id)
            and canary.status_value(o.side) == "buy" and float(o.filled_qty or 0) > 0 and o.filled_at]
    if not buys:
        return None
    return _et_day(max(buys, key=lambda o: canary._ts(o.filled_at)).filled_at)


def blocking_failures(report, today: str) -> list[str]:
    """Report failures that must stop the automation. Two are expected mid-protocol and ignored:
    'not flat' (a position held overnight) and today's own cash line (the session is not over)."""
    out = []
    for f in report["failures"]:
        if f.startswith("not flat:") or f.startswith(f"cash {today}:"):
            continue
        out.append(f)
    return out


def decide(snap, report, buy_day, today: str, halted: bool):
    """Pure. Returns (action, reason); action is one of stop | sell | buy | hold | done.
    `today` is kept for the report filter (today's unfinished cash line is not a failure)."""
    bad = blocking_failures(report, today)
    if bad:
        return "stop", "measurement/protocol failure: " + "; ".join(bad)[:400]
    if snap["canary_open"]:
        return "stop", f"{len(snap['canary_open'])} canary order(s) already open (should have been cancelled at close)"
    cp = snap["canary_position"]
    if cp and cp["class"] != "canary":
        return "stop", "position on the canary symbol is not entirely canary-made"
    if cp:
        if buy_day is None:
            return "stop", "canary position held but no filled canary buy found"
        return "sell", f"canary position bought {buy_day}: sell it"
    if report["completed_round_trips"] >= report["target_round_trips"]:
        return "done", f"{report['completed_round_trips']} of {report['target_round_trips']} round trips completed"
    if halted:
        return "hold", "halt file present: no new entries"
    return "buy", f"flat; round trip {report['completed_round_trips'] + 1} of {report['target_round_trips']}"


# ---------- broker plumbing -------------------------------------------------------------------
def gather(c):
    from datetime import date, datetime, time as dtime
    since_iso = datetime.combine(date(2026, 10, 1), dtime(0), tzinfo=safety_guard.ET).isoformat()
    orders_all = canary.orders(c, "all", [canary.CANARY_SYMBOL])
    fills = [f for f in canary.broker_fill_activities(c, since_iso) if f.get("symbol") == canary.CANARY_SYMBOL]
    snap = canary.snapshot(c)
    report = canary.build_report(orders_all, fills, canary.canary_log_rows(), canary.flat_problems(snap))
    return snap, report, last_buy_fill_day(orders_all)


def _run_cmd(fn, c, **kw):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = fn(c, SimpleNamespace(**kw))
    out = buf.getvalue()
    m = re.search(r'"client_order_id":\s*"(canary-[^"]+)"', out)
    return rc, out, (m.group(1) if m else None)


def wait_fill(c, coid) -> bool:
    """True once the order is filled; after FILL_WAIT_S the rest is cancelled and False returned."""
    deadline = time.time() + FILL_WAIT_S
    oid = None
    while time.time() < deadline:
        try:
            o = c.client.get_order_by_client_id(coid)
            oid = str(o.id)
            st = canary.status_value(o.status)
            if st == "filled":
                return True
            if st in ("canceled", "cancelled", "expired", "rejected"):
                return False
        except Exception:
            pass
        time.sleep(POLL_S)
    if oid:
        _run_cmd(canary.cmd_cancel, c, order_id=oid)
    return False


def today_et() -> str:
    return canary._now_et().date().isoformat()


def market_open(c) -> bool:
    clock = c.get_clock()
    return bool(clock is not None and getattr(clock, "is_open", False))


def _cents_down(px: float) -> float:
    """Price rounded down to a cent; round() first so 299.9 * 100 = 29989.999... stays 299.90."""
    return math.floor(round(px * 100, 6)) / 100


def _cents_up(px: float) -> float:
    return math.ceil(round(px * 100, 6)) / 100


def _sell(c) -> tuple[bool, str]:
    q = canary._quote(canary.CANARY_SYMBOL)
    if not q or not q["bid"] > 0:
        return False, "no usable quote for the sell limit; will retry next session"
    limit = _cents_down(q["bid"])
    rc, out, coid = _run_cmd(canary.cmd_sell, c, limit=limit)
    if rc != 0 or not coid:
        return False, "sell refused: " + out.strip()[-300:]
    if not wait_fill(c, coid):
        return False, f"sell at {limit} did not fill within {FILL_WAIT_S // 60} min; cancelled, retry next session"
    return True, f"sold at limit {limit}"


def _buy(c) -> tuple[bool, str]:
    last = ""
    for attempt in range(1, BUY_RETRIES + 1):
        q = canary._quote(canary.CANARY_SYMBOL)
        if q and q["ask"] > 0:
            limit = _cents_up(q["ask"])
            rc, out, coid = _run_cmd(canary.cmd_buy, c, usd=BUY_USD, limit=limit)
            if rc == 0 and coid:
                if wait_fill(c, coid):
                    return True, f"bought ${BUY_USD:g} at limit {limit}"
                return False, f"buy at {limit} did not fill within {FILL_WAIT_S // 60} min; cancelled"
            last = out.strip().splitlines()[-1] if out.strip() else "refused"
            if "spread" not in last and "quote" not in last:
                return False, "buy " + last[:300]
        else:
            last = "no usable quote"
        if attempt < BUY_RETRIES:
            time.sleep(BUY_RETRY_GAP_S)
    return False, f"buy not sent after {BUY_RETRIES} tries ({last[:200]})"


# ---------- status file for the dashboard -------------------------------------------------------
STATUS_FILE = canary.EVIDENCE_DIR / "auto-status.json"


def write_status(snap, report, action: str, why: str, extra: dict | None = None) -> None:
    """Snapshot for the dashboard's Auto test page. The engine only READS this file: it never
    imports the canary code or touches the live account (tests/test_canary.py isolation)."""
    import json
    try:
        canary.EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        body = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "enabled": enabled(), "action": action, "reason": why,
                "position": snap.get("canary_position") if snap else None,
                "open_orders": len(snap.get("canary_open") or []) if snap else None,
                "completed_round_trips": report.get("completed_round_trips") if report else None,
                "target_round_trips": report.get("target_round_trips") if report else None,
                "verdict": report.get("verdict") if report else None,
                "failures": (report or {}).get("failures", []),
                "legs": (report or {}).get("legs", []), "round_trips": (report or {}).get("round_trips", []),
                "sessions": (report or {}).get("sessions", {}), **(extra or {})}
        tmp = STATUS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, indent=1, default=str))
        tmp.replace(STATUS_FILE)
    except Exception:
        pass   # the dashboard copy must never break a run


# ---------- commands --------------------------------------------------------------------------
def cmd_plan(c) -> int:
    snap, report, buy_day = gather(c)
    action, why = decide(snap, report, buy_day, today_et(), safety_guard.halt_file().exists())
    write_status(snap, report, action, why, {"source": "plan"})
    print(f"enabled: {enabled()}  market open: {market_open(c)}  "
          f"round trips: {report['completed_round_trips']} of {report['target_round_trips']}")
    print(f"next action: {action.upper()}  ({why})")
    return 0


def cmd_run(c) -> int:
    if not enabled():
        write_status(None, None, "off", "CANARY_AUTO is not 1", {"source": "run"})
        print("CANARY_AUTO is not 1: doing nothing.")
        return 0
    if not market_open(c):
        _log("run", decision="skip", reason="market closed")
        print("market closed: nothing to do.")
        return 0
    done, bought = [], False
    for _ in range(3):   # at most: sell a leftover position, buy, then sell that buy (same day)
        snap, report, buy_day = gather(c)
        action, why = decide(snap, report, buy_day, today_et(), safety_guard.halt_file().exists())
        _log("decide", decision=action, reason=why)
        if action == "stop":
            write_status(snap, report, action, why, {"source": "run"})
            _notify(f"STOPPED, nothing sent. {why}\nInvestigate, then fix or set CANARY_AUTO=0.")
            print("STOP: " + why)
            return 2
        if action in ("hold", "done"):
            if action == "done" and not done:
                _notify(f"target reached ({why}). Run `canary.py report` and set CANARY_AUTO=0.")
            done.append(f"{action}: {why}")
            break
        if action == "buy" and bought:
            break   # never a second buy in one run, even if a lagging position read says "flat"
        bought = bought or action == "buy"
        ok, msg = _sell(c) if action == "sell" else _buy(c)
        _log(action, decision="ok" if ok else "not_done", reason=msg)
        done.append(msg)
        if not ok:
            break
    snap, report, _ = gather(c)
    summary = " · ".join(done) + f" · round trips {report['completed_round_trips']} of {report['target_round_trips']}"
    write_status(snap, report, "ran", summary, {"source": "run", "steps": done})
    _notify(summary)
    print(summary)
    return 0


def cmd_close(c) -> int:
    if not enabled():
        print("CANARY_AUTO is not 1: doing nothing.")
        return 0
    snap = canary.snapshot(c)
    if snap["canary_open"]:
        _run_cmd(canary.cmd_cancel, c, order_id=None)
    # Records the end-of-session cash the pre-registered cash check needs (logged even when holding).
    rc, out, _ = _run_cmd(canary.cmd_assert_flat, c)
    cp = canary.snapshot(c)["canary_position"]
    msg = f"close: holding {cp['qty']} {cp['symbol']} overnight" if cp else "close: flat"
    _log("close", decision="holding" if cp else "flat", reason=msg)
    try:
        snap2, report, _ = gather(c)
        write_status(snap2, report, "closed", msg, {"source": "close"})
    except Exception:
        pass
    print(msg)
    return 0


def main(argv=None) -> int:
    cmd = (argv if argv is not None else sys.argv[1:] or ["plan"])[0]
    if cmd not in ("run", "close", "plan"):
        print(__doc__)
        return 1
    c = canary.live_client()
    if c.client is None:
        print("live client unavailable (APCA_LIVE_API_KEY_ID / APCA_LIVE_API_SECRET_KEY).")
        return 1
    try:
        return {"run": cmd_run, "close": cmd_close, "plan": cmd_plan}[cmd](c)
    except Exception as e:   # a crash must be visible, never a silent half-run
        _notify(f"CRASHED: {type(e).__name__}: {str(e)[:200]}")
        raise


if __name__ == "__main__":
    sys.exit(main())
