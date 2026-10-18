"""Micro-live phase 1 canary (strategy.md, "Micro-live phase 1 (canary)").

Execution-only test of the LIVE Alpaca account at minimum size. No strategy signal drives any
order here, and the engine is not involved: aether_engine.py keeps US_LIVE=false and never sees
the live account. This script has its own live client but goes through the SAME executor
(alpaca_executor), the SAME risk_controller (under its own "canary" key and caps) and the SAME
safety_guard live ledger as the engine, because it loads the same .env before importing them.

Every canary order's client_order_id starts with "canary-". Nothing is kept locally: each run
re-reads the broker and classifies orders/positions from client_order_id alone, so every
invocation is a cold start.

  python3 canary.py preflight                 # read-only; must pass before any order
  python3 canary.py status                    # canary orders/positions, classified from the broker
  python3 canary.py buy  --usd 8 --limit 312.40
  python3 canary.py sell --limit 312.10       # sells the whole canary position
  python3 canary.py cancel [ORDER_ID]         # cancels canary open orders (all, or one)
  python3 canary.py assert-flat               # exit 0 only if no canary position and no open order
  python3 canary.py report [--since DATE]     # pre-registered execution measurement: PASS/FAIL

Run from the repo directory as the service user. Every session ends with `assert-flat`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _load_env_file(path: Path) -> None:
    """Load the engine's .env before importing risk_controller/safety_guard: both read their
    paths (halt file, risk state, AETHER_HOME ledger) from the environment, and the canary must
    see exactly the files the engine sees. Existing environment variables win."""
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
    _load_env_file(_HERE / ".env.live")   # live Alpaca keys, if split out of .env (optional)

import risk_controller  # noqa: E402
import safety_guard  # noqa: E402
import us_strategy  # noqa: E402
from alpaca_executor import AlpacaExecutionClient, status_value  # noqa: E402

# Canary symbols must be OUTSIDE the strategy universe. A leftover canary position or resting
# order on a universe ETF (SPY is one) would make the engine refuse its first live rebalance
# ("refuses to take over unknown positions" / "open orders pending"). VTI is not in
# us_strategy.UNIVERSE; the assert below fails at import if that ever changes.
CANARY_SYMBOL = "VTI"
assert CANARY_SYMBOL not in us_strategy.UNIVERSE, "canary symbol must be outside the strategy universe"
PREFIX = "canary-"


def new_coid(side: str) -> str:
    return f"{PREFIX}{side.lower()}-{uuid.uuid4().hex}"  # 7 + 4/5 + 32 chars: well under 128


def is_canary(coid) -> bool:
    return str(coid or "").startswith(PREFIX)


def live_client() -> AlpacaExecutionClient:
    return AlpacaExecutionClient(paper=False, key_env="APCA_LIVE_API_KEY_ID",
                                 secret_env="APCA_LIVE_API_SECRET_KEY")


def _log(event, **kw):
    try:
        import decision_log
        decision_log.log("canary", event, mode="live", **kw)
    except Exception:
        pass


# ---------- broker reads (all classification comes from here) ------------------------------
def orders(c, status="open", symbols=None):
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
    st = {"open": QueryOrderStatus.OPEN, "all": QueryOrderStatus.ALL}[status]
    req = GetOrdersRequest(status=st, symbols=symbols, limit=500, nested=False)
    return c.client.get_orders(req)


def snapshot(c) -> dict:
    """Everything a session decision needs, read fresh from the broker. Raises on any read
    failure: a canary never acts on partial state."""
    positions = {p.symbol: float(p.qty) for p in c.client.get_all_positions()}
    open_orders = orders(c, "open")
    canary_open = [o for o in open_orders if is_canary(o.client_order_id)]
    foreign_open = [o for o in open_orders if not is_canary(o.client_order_id)]
    canary_pos = None
    if positions.get(CANARY_SYMBOL):
        hist = [o for o in orders(c, "all", [CANARY_SYMBOL]) if float(o.filled_qty or 0) > 0]
        # Position -> order mapping is canary-only: one-in-one-out means every fill on the canary
        # symbol came from a canary order. Any non-canary fill makes the position "unknown".
        tag = "canary" if hist and all(is_canary(o.client_order_id) for o in hist) else "unknown"
        canary_pos = {"symbol": CANARY_SYMBOL, "qty": positions[CANARY_SYMBOL], "class": tag}
    strategy_pos = {s: q for s, q in positions.items() if s in us_strategy.UNIVERSE and q}
    other_pos = {s: q for s, q in positions.items()
                 if s not in us_strategy.UNIVERSE and s != CANARY_SYMBOL and q}
    return {"positions": positions, "canary_position": canary_pos,
            "canary_open": canary_open, "foreign_open": foreign_open,
            "strategy_positions": strategy_pos, "other_positions": other_pos}


def _order_row(o) -> dict:
    return {"id": str(o.id), "client_order_id": o.client_order_id, "symbol": o.symbol,
            "side": status_value(o.side), "qty": str(o.qty), "limit": str(o.limit_price),
            "state": status_value(o.status), "filled_qty": str(o.filled_qty)}


# ---------- checks -------------------------------------------------------------------------
def preflight_checks(c, acct, snap) -> list[tuple[str, bool, str]]:
    """(name, ok, detail). Pure: takes broker objects, reads the ledger and env."""
    out = []
    out.append(("US_LIVE is false (engine not live; canaries are phase 1 only)",
                os.getenv("US_LIVE", "false").lower() != "true", os.getenv("US_LIVE", "false")))
    out.append(("live client (paper=False)", c.paper is False, "LIVE" if not c.paper else "PAPER"))
    status = status_value(getattr(acct, "status", ""))
    out.append(("account active", status == "active", status))
    equity = float(getattr(acct, "equity", 0) or 0)
    out.append(("account funded (wire credited)", equity > 0, f"equity ${equity:,.2f}"))
    mult = str(getattr(acct, "multiplier", "?"))
    kind = "cash" if mult == "1" else f"margin (multiplier {mult})"
    out.append(("account type recorded", mult != "?", kind))
    if mult != "1":
        dt = int(getattr(acct, "daytrade_count", 0) or 0)
        out.append(("margin: day trades in last 5 sessions <= 2 (room for one more)", dt <= 2, str(dt)))
    out.append(("no strategy-universe positions on the live account", not snap["strategy_positions"],
                json.dumps(snap["strategy_positions"])))
    out.append(("no non-canary open orders on the live account", not snap["foreign_open"],
                f"{len(snap['foreign_open'])} open"))
    cp = snap["canary_position"]
    out.append(("no unknown position on the canary symbol", not cp or cp["class"] == "canary",
                json.dumps(cp)))
    hf, rf = safety_guard.halt_file(), Path(risk_controller.HALT_FILE).absolute()
    out.append(("one halt-file path (risk_controller == safety_guard)", hf == rf,
                f"risk_controller {rf} | safety_guard {hf}"))
    legacy = safety_guard.legacy_halt_file()
    out.append(("no stale halt file at the legacy breaker path", legacy == hf or not legacy.exists(),
                f"{legacy} {'EXISTS' if legacy.exists() else 'absent'}"))
    L = safety_guard.load_ledger()
    last = L.get("last_mark")
    out.append(("live risk ledger initialised (risk_admin.py mark)", bool(last), json.dumps(last)))
    if last:
        from datetime import date, datetime
        age = (datetime.now(safety_guard.ET).date() - date.fromisoformat(last["date"])).days
        lim = safety_guard.limits()
        out.append(("risk mark fresh", age <= lim.max_mark_age_days, f"{age} day(s) old"))
        drift = abs(last["equity"] - equity)
        out.append(("ledger equity matches broker (no double-counted deposit)",
                    drift <= max(1.0, 0.02 * equity), f"ledger ${last['equity']:,.2f} vs broker ${equity:,.2f}"))
        out.append(("no breaker latched", not L.get("latched"), json.dumps(L.get("latched"))))
    return out


def margin_day_trade_room(acct) -> bool:
    if str(getattr(acct, "multiplier", "1")) == "1":
        return True
    return int(getattr(acct, "daytrade_count", 0) or 0) <= 2


# ---------- commands -------------------------------------------------------------------------
def cmd_preflight(c, args) -> int:
    acct = c.client.get_account()
    snap = snapshot(c)
    rows = preflight_checks(c, acct, snap)
    for name, ok, detail in rows:
        print(f"{'PASS' if ok else 'FAIL'}  {name}  [{detail}]")
    bad = [n for n, ok, _ in rows if not ok]
    _log("preflight", decision="pass" if not bad else "fail", reason="; ".join(bad) or "all checks passed")
    print("\nPREFLIGHT " + ("PASSED" if not bad else f"FAILED ({len(bad)})"))
    return 0 if not bad else 1


def cmd_status(c, args) -> int:
    snap = snapshot(c)
    print(json.dumps({"canary_position": snap["canary_position"],
                      "canary_open_orders": [_order_row(o) for o in snap["canary_open"]],
                      "non_canary_open_orders": len(snap["foreign_open"]),
                      "strategy_positions": snap["strategy_positions"],
                      "other_positions": snap["other_positions"]}, indent=2))
    return 0


def _quote(symbol):
    """Latest NBBO from the live keys' data feed, for the slippage record and the price band.
    None if unavailable: the band is then skipped, never guessed."""
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestQuoteRequest
        dc = StockHistoricalDataClient(os.environ["APCA_LIVE_API_KEY_ID"], os.environ["APCA_LIVE_API_SECRET_KEY"])
        q = dc.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbol))[symbol]
        return {"bid": float(q.bid_price), "ask": float(q.ask_price), "t": str(q.timestamp)}
    except Exception:
        return None


def cmd_buy(c, args) -> int:
    acct = c.client.get_account()
    snap = snapshot(c)
    failed = [n for n, ok, _ in preflight_checks(c, acct, snap) if not ok]
    if failed:
        print("refused: preflight failing -> " + "; ".join(failed))
        return 1
    if snap["canary_position"] or snap["canary_open"]:
        print("refused: one-in-one-out. A canary position or open order already exists; "
              "sell/cancel it first (status shows it).")
        return 1
    if not margin_day_trade_room(acct):
        print("refused: margin account with no day-trade room; wait for the 5-session window to roll.")
        return 1
    window = _session_window_problem(c)
    if window:
        print(f"refused: {window}")
        return 1
    q = _quote(CANARY_SYMBOL)
    if not q or not q["ask"] > 0:
        # Fail closed: without a quote the price band cannot be checked, so a typo'd limit
        # (312.40 vs 3124.0) would go straight to the live account.
        print("refused: no usable quote for the canary symbol, so the 1% price band cannot be checked.")
        return 1
    if args.limit > q["ask"] * 1.01:
        print(f"refused: limit {args.limit} is >1% above ask {q['ask']} (price band).")
        return 1
    if not q["bid"] > 0 or q["ask"] < q["bid"]:
        print(f"refused: unusable quote (bid {q['bid']}, ask {q['ask']}).")
        return 1
    spread_bp = (q["ask"] - q["bid"]) / ((q["ask"] + q["bid"]) / 2) * 1e4
    if spread_bp > PREREG["max_spread_bp"]:
        # Same line the report applies: an order sent now would be a guaranteed FAIL.
        print(f"refused: spread {spread_bp:.1f} bp > pre-registered {PREREG['max_spread_bp']:g} bp; "
              f"wait for a tighter quote.")
        return 1
    qty = round(args.usd / args.limit, 6)
    coid = new_coid("buy")
    r = c._canary_submit_limit_order(CANARY_SYMBOL, "BUY", qty, args.limit, coid)
    _log("order", symbol=CANARY_SYMBOL, decision=r.get("status"), price=args.limit,
         reason=f"BUY {qty} limit {args.limit} coid {coid}: {r.get('message', 'ok')}", quote=q,
         client_order_id=coid, side="buy", cash_before=_cash(acct))
    print(json.dumps({"result": r, "client_order_id": coid, "quote_at_submit": q}, indent=2))
    return 0 if r.get("status") == "submitted" else 1


def cmd_sell(c, args) -> int:
    snap = snapshot(c)
    cp = snap["canary_position"]
    if not cp:
        print("nothing to sell: no canary position.")
        return 1
    if cp["class"] != "canary":
        print("refused: the position on the canary symbol is not entirely canary-made. Investigate by hand.")
        return 1
    if snap["canary_open"]:
        print("refused: a canary order is still open; cancel it first.")
        return 1
    q = _quote(CANARY_SYMBOL)   # recorded for the measurement; a missing quote never blocks an exit
    coid = new_coid("sell")
    r = c._canary_submit_limit_order(CANARY_SYMBOL, "SELL", cp["qty"], args.limit, coid)
    _log("order", symbol=CANARY_SYMBOL, decision=r.get("status"), price=args.limit,
         reason=f"SELL {cp['qty']} limit {args.limit} coid {coid}: {r.get('message', 'ok')}", quote=q,
         client_order_id=coid, side="sell", cash_before=_cash_or_none(c))
    print(json.dumps({"result": r, "client_order_id": coid, "quote_at_submit": q}, indent=2))
    return 0 if r.get("status") == "submitted" else 1


def cmd_cancel(c, args) -> int:
    snap = snapshot(c)
    targets = [o for o in snap["canary_open"] if not args.order_id or str(o.id) == args.order_id]
    if args.order_id and not targets:
        print("refused: that id is not an open canary order (non-canary orders are never touched).")
        return 1
    ok = all(c.cancel_order(str(o.id)) for o in targets)
    _log("cancel", decision="ok" if ok else "partial", reason=f"{len(targets)} canary order(s)")
    print(f"cancel requested for {len(targets)} canary order(s): {'ok' if ok else 'SOME FAILED'}")
    return 0 if ok else 1


def flat_problems(snap) -> list[str]:
    p = []
    if snap["canary_position"]:
        p.append(f"canary position open: {snap['canary_position']}")
    if snap["canary_open"]:
        p.append(f"{len(snap['canary_open'])} canary order(s) still open")
    return p


def cmd_assert_flat(c, args) -> int:
    probs = flat_problems(snapshot(c))
    _log("assert_flat", decision="flat" if not probs else "not_flat", reason="; ".join(probs) or "flat",
         cash=_cash_or_none(c))
    print("FLAT" if not probs else "NOT FLAT: " + "; ".join(probs))
    return 0 if not probs else 1


# ---------- pre-registered execution measurement (strategy.md 10, "Execution measurement") ---------
# These numbers are the pass/fail line, fixed BEFORE the first live canary order. They are module
# constants on purpose -- not .env settings -- so they can only change by a commit (the git history
# is the pre-registration record), and tests/test_canary_report.py fails if they drift from the
# copy in strategy.md. Change both together, in a commit made BEFORE the session it applies to.
PREREG = {
    "registered": "2026-10-17",
    "max_fill_seconds": 900,          # submit -> filled, per leg
    "max_abs_shortfall_bp": 10.0,     # |fill vs mid-at-submit|, signed so + = cost, per leg
    "max_spread_bp": 10.0,            # (ask - bid) / mid at submit, per leg. Must be <= 2x the
                                      # shortfall line: crossing the spread costs half of it, so at
                                      # 10 bp a buy at the ask starts at 5 bp, leaving 5 bp for drift.
                                      # buy refuses above it BEFORE sending (no doomed orders).
    "max_cash_residual_usd": 0.05,    # per session: broker cash change vs fills (+ allowed fees)
    "window_et": ["10:00", "15:45"],  # canary buys only inside this window, Mon-Fri, market open
    "target_round_trips": 10,         # section 10 "Conclusive when"
}
EVIDENCE_DIR = _HERE / "tests" / "evidence" / "canary"


def _now_et():
    from datetime import datetime
    return datetime.now(safety_guard.ET)


def _session_window_problem(c, now=None):
    """None if a canary BUY may go out now, else the reason. Exits are never subject to this.
    Fails closed when the market clock cannot be read."""
    now = now or _now_et()
    lo, hi = (tuple(int(x) for x in t.split(":")) for t in PREREG["window_et"])
    if now.weekday() >= 5 or not (lo <= (now.hour, now.minute) <= hi):
        return (f"outside the pre-registered window {PREREG['window_et'][0]}-{PREREG['window_et'][1]} ET "
                f"Mon-Fri (now {now:%a %H:%M} ET)")
    clock = c.get_clock()
    if clock is None:
        return "market clock unavailable; not sending a buy blind"
    if not getattr(clock, "is_open", False):
        return "market is closed today (holiday or early close)"
    return None


def _cash(acct):
    try:
        return float(acct.cash)
    except (AttributeError, TypeError, ValueError):
        return None


def _cash_or_none(c):
    try:
        return _cash(c.client.get_account())
    except Exception:
        return None


def _ts(x):
    """Broker timestamp (datetime or ISO string) -> aware datetime, or None."""
    from datetime import datetime, timezone
    if x is None or x == "":
        return None
    if isinstance(x, datetime):
        return x if x.tzinfo else x.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(x).replace("Z", "+00:00"))


def canary_log_rows(db_path=None):
    """Canary rows from the decision log, oldest first: [{'ts','event','data'}]. Read-only."""
    import sqlite3
    import decision_log
    db = db_path or decision_log.DB_PATH
    if not Path(db).exists():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
    try:
        rows = con.execute("SELECT ts, event, data_json FROM decisions WHERE engine='canary' "
                           "ORDER BY id").fetchall()
    finally:
        con.close()
    return [{"ts": ts, "event": ev, "data": json.loads(dj) if dj else {}} for ts, ev, dj in rows]


def broker_fill_activities(c, since_iso):
    """Raw FILL activities from Alpaca (/v2/account/activities/FILL), oldest first, all pages."""
    out, token = [], None
    for _ in range(50):
        params = {"direction": "asc", "page_size": 100, "after": since_iso}
        if token:
            params["page_token"] = token
        page = c.client.get("/account/activities/FILL", params) or []
        out.extend(page)
        if len(page) < 100:
            break
        token = page[-1]["id"]
    return out


def leg_metrics(order, fills, quote, prereg=PREREG):
    """One filled canary order, measured from the broker's own records.
    order: Alpaca Order (or same-shaped object); fills: its FILL activities; quote: {'bid','ask'} at submit."""
    side = status_value(order.side)
    sign = 1 if side == "buy" else -1
    problems = []
    fq = sum(float(f["qty"]) for f in fills)
    vwap = (sum(float(f["qty"]) * float(f["price"]) for f in fills) / fq) if fq > 0 else None
    oq = float(order.filled_qty or 0)
    op = float(order.filled_avg_price) if order.filled_avg_price else None
    if not fills:
        problems.append("no FILL activity for a filled order")
    elif abs(fq - oq) > 1e-9:
        problems.append(f"fill qty {fq} != order filled_qty {oq}")
    elif op is None or abs(vwap - op) > max(1e-4, 1e-6 * op):
        problems.append(f"fill VWAP {vwap} != order filled_avg_price {op}")
    sub, filled = _ts(order.submitted_at), _ts(order.filled_at)
    fill_s = (filled - sub).total_seconds() if sub and filled else None
    if fill_s is None:
        problems.append("missing submitted_at/filled_at")
    elif fill_s > prereg["max_fill_seconds"]:
        problems.append(f"time to fill {fill_s:.0f}s > {prereg['max_fill_seconds']}s")
    shortfall_bp = spread_bp = mid = None
    if not quote or not (quote.get("bid", 0) > 0 and quote.get("ask", 0) >= quote.get("bid", 0)):
        problems.append("no usable quote recorded at submit")
    else:
        mid = (quote["bid"] + quote["ask"]) / 2
        spread_bp = (quote["ask"] - quote["bid"]) / mid * 1e4
        if spread_bp > prereg["max_spread_bp"]:
            problems.append(f"spread at submit {spread_bp:.1f}bp > {prereg['max_spread_bp']}bp")
        px = vwap if vwap is not None else op
        if px is not None:
            shortfall_bp = sign * (px - mid) / mid * 1e4
            if abs(shortfall_bp) > prereg["max_abs_shortfall_bp"]:
                problems.append(f"|shortfall| {abs(shortfall_bp):.1f}bp > {prereg['max_abs_shortfall_bp']}bp")
    return {"client_order_id": order.client_order_id, "order_id": str(order.id), "side": side,
            "qty": oq, "fill_price": vwap if vwap is not None else op, "mid_at_submit": mid,
            "spread_bp": spread_bp, "shortfall_bp": shortfall_bp, "time_to_fill_s": fill_s,
            "submitted_at": sub and sub.isoformat(), "filled_at": filled and filled.isoformat(),
            "cash_flow": (-1 if side == "buy" else 1) * fq * (vwap or 0.0),
            "pass": not problems, "problems": problems}


def round_trips(legs):
    """Pair filled legs buy -> next sell, in fill order. Anything else is a protocol violation
    (one in, one out): two buys in a row, or a sell with no open buy."""
    trips, open_buy, violations = [], None, []
    for leg in sorted((x for x in legs if x["filled_at"]), key=lambda x: _ts(x["filled_at"])):
        if leg["side"] == "buy":
            if open_buy is not None:
                violations.append(f"second buy {leg['client_order_id']} before selling {open_buy['client_order_id']}")
            open_buy = leg
        else:
            if open_buy is None:
                violations.append(f"sell {leg['client_order_id']} with no open canary buy")
                continue
            trips.append({"buy": open_buy["client_order_id"], "sell": leg["client_order_id"],
                          "pnl_usd": round(open_buy["cash_flow"] + leg["cash_flow"], 4),
                          "pass": open_buy["pass"] and leg["pass"]})
            open_buy = None
    return trips, violations, open_buy


def session_cash(log_rows, legs, prereg=PREREG):
    """Per ET session: (cash at assert-flat) - (cash before the session's first canary order) must
    equal the session's canary fill cash flows, within max_cash_residual_usd (regulatory fees).
    Any other cash movement that day (deposit, fee, stray trade) shows up as a residual -> FAIL."""
    from collections import defaultdict
    by_day = defaultdict(lambda: {"start": None, "end": None, "flow": 0.0, "legs": 0})
    for r in log_rows:
        d = r["data"]
        day = _ts(r["ts"]).astimezone(safety_guard.ET).date().isoformat()
        if r["event"] == "order" and d.get("cash_before") is not None and by_day[day]["start"] is None:
            by_day[day]["start"] = float(d["cash_before"])
        if r["event"] == "assert_flat" and d.get("cash") is not None:
            by_day[day]["end"] = float(d["cash"])   # last one of the day wins
    for leg in legs:
        if not leg["filled_at"]:
            continue
        day = _ts(leg["filled_at"]).astimezone(safety_guard.ET).date().isoformat()
        by_day[day]["flow"] += leg["cash_flow"]
        by_day[day]["legs"] += 1
    out = {}
    for day, s in sorted(by_day.items()):
        if not s["legs"]:
            continue
        if s["start"] is None or s["end"] is None:
            out[day] = {"pass": False, "problem": "cash not recorded at session start and/or assert-flat",
                        **{k: s[k] for k in ("start", "end")}, "fills_flow": round(s["flow"], 4)}
            continue
        residual = (s["end"] - s["start"]) - s["flow"]
        out[day] = {"start": s["start"], "end": s["end"], "fills_flow": round(s["flow"], 4),
                    "residual_usd": round(residual, 4),
                    "pass": abs(residual) <= prereg["max_cash_residual_usd"] + 1e-9}
    return out


def build_report(orders_all, fills, log_rows, flat_problems_now, since=None, prereg=PREREG):
    """Pure: everything comes in as broker/log data, the verdict comes out."""
    quotes = {r["data"].get("client_order_id"): r["data"].get("quote")
              for r in log_rows if r["event"] == "order" and r["data"].get("client_order_id")}
    def _day(o):
        t = _ts(o.submitted_at)
        return t.astimezone(safety_guard.ET).date().isoformat() if t else ""
    canary_orders = [o for o in orders_all if is_canary(o.client_order_id) and (since is None or _day(o) >= since)]
    by_order = {}
    for f in fills:
        by_order.setdefault(str(f.get("order_id")), []).append(f)
    legs, not_filled = [], []
    for o in canary_orders:
        if float(o.filled_qty or 0) > 0:
            legs.append(leg_metrics(o, by_order.get(str(o.id), []), quotes.get(o.client_order_id), prereg))
        else:
            not_filled.append({"client_order_id": o.client_order_id, "state": status_value(o.status)})
    trips, violations, open_buy = round_trips(legs)
    sessions = session_cash(log_rows, legs, prereg)
    failures = [f"{l['side']} {l['client_order_id']}: {p}" for l in legs for p in l["problems"]]
    failures += [f"protocol: {v}" for v in violations]
    failures += [f"cash {d}: {s.get('problem') or ('residual $%.4f' % s['residual_usd'])}"
                 for d, s in sessions.items() if not s["pass"]]
    failures += [f"not flat: {p}" for p in flat_problems_now]
    return {"prereg": prereg, "since": since, "legs": legs, "round_trips": trips,
            "completed_round_trips": len(trips), "target_round_trips": prereg["target_round_trips"],
            "open_buy": open_buy and open_buy["client_order_id"], "not_filled": not_filled,
            "sessions": sessions, "failures": failures,
            "verdict": "PASS" if legs and not failures else ("NO DATA" if not legs else "FAIL")}


def cmd_report(c, args) -> int:
    from datetime import datetime, timezone
    from datetime import date, time as dtime
    since = args.since
    start = date.fromisoformat(since) if since else date(2026, 10, 1)   # canaries began Oct 2026
    since_iso = datetime.combine(start, dtime(0), tzinfo=safety_guard.ET).isoformat()
    orders_all = orders(c, "all", [CANARY_SYMBOL])
    fills = [f for f in broker_fill_activities(c, since_iso) if f.get("symbol") == CANARY_SYMBOL]
    rep = build_report(orders_all, fills, canary_log_rows(), flat_problems(snapshot(c)), since=since)
    rep["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / f"report-{datetime.now(safety_guard.ET):%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(rep, indent=2, default=str))
    for l in rep["legs"]:
        f = lambda v, fmt: "n/a" if v is None else format(v, fmt)
        print(f"{l['side'].upper():4} {l['client_order_id'][:22]:22}  fill {f(l['fill_price'], '.4f')}  "
              f"shortfall {f(l['shortfall_bp'], '+.1f')}bp  spread {f(l['spread_bp'], '.1f')}bp  "
              f"fill {f(l['time_to_fill_s'], '.0f')}s  {'ok' if l['pass'] else 'FAIL'}")
    for d, s in rep["sessions"].items():
        print(f"cash {d}: " + (f"residual ${s['residual_usd']:+.4f}" if "residual_usd" in s else s["problem"])
              + ("  ok" if s["pass"] else "  FAIL"))
    print(f"round trips: {rep['completed_round_trips']} of {rep['target_round_trips']}")
    for x in rep["failures"]:
        print("  - " + x)
    print("\nAlso compare by hand with Alpaca's trade confirmation / statement for each session "
          "(qty, price, fees): the one check that is not our own code.")
    print(f"\nCANARY MEASUREMENT: {rep['verdict']}   (evidence: {path})")
    _log("report", decision=rep["verdict"], reason="; ".join(rep["failures"])[:500] or "all checks passed",
         round_trips=rep["completed_round_trips"])
    return 0 if rep["verdict"] == "PASS" else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preflight")
    sub.add_parser("status")
    b = sub.add_parser("buy")
    b.add_argument("--usd", type=float, required=True)
    b.add_argument("--limit", type=float, required=True)
    s = sub.add_parser("sell")
    s.add_argument("--limit", type=float, required=True)
    x = sub.add_parser("cancel")
    x.add_argument("order_id", nargs="?")
    sub.add_parser("assert-flat")
    rp = sub.add_parser("report")
    rp.add_argument("--since", help="ET date YYYY-MM-DD (default: all canary history)")
    args = ap.parse_args(argv)
    c = live_client()
    if c.client is None:
        print("live client unavailable (APCA_LIVE_API_KEY_ID / APCA_LIVE_API_SECRET_KEY).")
        return 1
    fn = {"preflight": cmd_preflight, "status": cmd_status, "buy": cmd_buy, "sell": cmd_sell,
          "cancel": cmd_cancel, "assert-flat": cmd_assert_flat, "report": cmd_report}[args.cmd]
    return fn(c, args)


if __name__ == "__main__":
    sys.exit(main())
