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
    q = _quote(CANARY_SYMBOL)
    if not q or not q["ask"] > 0:
        # Fail closed: without a quote the price band cannot be checked, so a typo'd limit
        # (312.40 vs 3124.0) would go straight to the live account.
        print("refused: no usable quote for the canary symbol, so the 1% price band cannot be checked.")
        return 1
    if args.limit > q["ask"] * 1.01:
        print(f"refused: limit {args.limit} is >1% above ask {q['ask']} (price band).")
        return 1
    qty = round(args.usd / args.limit, 6)
    coid = new_coid("buy")
    r = c._canary_submit_limit_order(CANARY_SYMBOL, "BUY", qty, args.limit, coid)
    _log("order", symbol=CANARY_SYMBOL, decision=r.get("status"), price=args.limit,
         reason=f"BUY {qty} limit {args.limit} coid {coid}: {r.get('message', 'ok')}", quote=q)
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
    q = _quote(CANARY_SYMBOL)
    coid = new_coid("sell")
    r = c._canary_submit_limit_order(CANARY_SYMBOL, "SELL", cp["qty"], args.limit, coid)
    _log("order", symbol=CANARY_SYMBOL, decision=r.get("status"), price=args.limit,
         reason=f"SELL {cp['qty']} limit {args.limit} coid {coid}: {r.get('message', 'ok')}", quote=q)
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
    _log("assert_flat", decision="flat" if not probs else "not_flat", reason="; ".join(probs) or "flat")
    print("FLAT" if not probs else "NOT FLAT: " + "; ".join(probs))
    return 0 if not probs else 1


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
    args = ap.parse_args(argv)
    c = live_client()
    if c.client is None:
        print("live client unavailable (APCA_LIVE_API_KEY_ID / APCA_LIVE_API_SECRET_KEY).")
        return 1
    fn = {"preflight": cmd_preflight, "status": cmd_status, "buy": cmd_buy, "sell": cmd_sell,
          "cancel": cmd_cancel, "assert-flat": cmd_assert_flat}[args.cmd]
    return fn(c, args)


if __name__ == "__main__":
    sys.exit(main())
