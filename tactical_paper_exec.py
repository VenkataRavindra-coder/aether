"""Automatic PAPER execution of the tactical strategies (owner request 2026-10-23).

Runs every minute in US hours on the stress-test PAPER account and trades QQQ when the frozen
tactical rules fire, so the owner can watch the strategies trade without typing orders:

- A `orb15` (opening range breakout) and B `engulf15` (bullish engulfing): the engine modules in
  aether_tactical/ decide, unchanged. When a rule reports `state == "signal"` (the signal bar just
  closed; entry is the next bar), this places a MARKET buy with a broker-held stop-loss
  (ORB: OTO with stop = range low; engulfing: bracket with stop = bar low and target = entry + 1.5R).
- Size: fixed risk RISK_USD per trade -> shares = floor(RISK_USD / (price - stop)), whole shares.
- One trade per strategy per day. A missed signal is never chased.
- `close` (15:55 New York): cancels this module's open QQQ orders and sells the whole QQQ position.
- Every action goes to the decision log (engine "tactical_exec") and Telegram.

Safety: PAPER ONLY, hard-coded (paper=True, STRESS_ALPACA_* keys; refuses to start if those keys are
missing). QQQ only: the stress test trades SPY and BRK.B on the same account and closes them.
It ignores the live halt file on purpose (paper practice); its own switch is TACTICAL_PAPER_EXEC=1.
Never touches the live account, the engine's paper account, or the Risk Controller's live ledger.

  python3 tactical_paper_exec.py tick     # timer, every minute 09:46-15:54 New York
  python3 tactical_paper_exec.py close    # timer, 15:55 New York
  python3 tactical_paper_exec.py status   # read-only
"""
from __future__ import annotations

import json
import math
import os
import sys
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
SYMBOL = "QQQ"
RISK_USD = float(os.getenv("TACTICAL_PAPER_RISK_USD", "50"))
PREFIX = "tpx-"
STATE = Path(os.getenv("TACTICAL_EXEC_STATE", str(HERE / "tactical_exec_state.json")))
MAX_SIGNAL_AGE_MIN = 2          # enter only if the signal bar closed within this many minutes


def _load_env():
    for name in (".env",):
        f = HERE / name
        if f.exists():
            for line in f.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ---------------------------------------------------------------- pure decisions
def plan_entry(result: dict, last_bar_t, now, price: float, risk_usd: float = RISK_USD):
    """Pure. From one engine result, the order to place now, or (None, reason)."""
    if result.get("state") != "signal":
        return None, f"no fresh signal ({result.get('state')})"
    if last_bar_t is None or now - (last_bar_t + timedelta(minutes=1)) > timedelta(minutes=MAX_SIGNAL_AGE_MIN):
        return None, "signal is stale: not chasing"
    name = result["strategy"]
    if name == "orb15":
        stop = float(result["range_low"])
    else:   # engulf15: the engulfing bar's low is the stop; recompute from the reason-free fields
        stop = float(result["stop"]) if "stop" in result else None
        if stop is None:
            return None, "engulfing stop unknown"
    risk = price - stop
    if not (risk > 0.01):
        return None, f"price {price:.2f} not above stop {stop:.2f}"
    qty = math.floor(risk_usd / risk)
    if qty < 1:
        return None, f"risk per share {risk:.2f} > budget {risk_usd:.0f}"
    order = {"strategy": name, "qty": qty, "stop": round(stop, 2), "est_price": price}
    if name == "engulf15":
        order["target"] = round(price + 1.5 * risk, 2)
    return order, "enter"


# ---------------------------------------------------------------- state
def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def save_state(s: dict):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1))
    tmp.replace(STATE)


def _log(event, decision, reason, **data):
    try:
        import decision_log
        decision_log.log("tactical_exec", event, mode="paper", symbol=SYMBOL, decision=decision, reason=reason, **data)
    except Exception:
        pass


def _tell(text):
    try:
        import notify
        notify.send_sync("🧪 Paper auto-trade (stress-test account, no real money)\n" + text)
    except Exception:
        pass


# ---------------------------------------------------------------- broker
def client():
    k, s = os.getenv("STRESS_ALPACA_API_KEY", ""), os.getenv("STRESS_ALPACA_SECRET_KEY", "")
    if not k or not s:
        raise SystemExit("STRESS_ALPACA_API_KEY / STRESS_ALPACA_SECRET_KEY missing: refusing to run")
    from alpaca.trading.client import TradingClient
    return TradingClient(k, s, paper=True)            # PAPER ONLY, never changed


def submit(tc, order: dict, day: str):
    from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest, StopLossRequest, TakeProfitRequest
    kw = dict(symbol=SYMBOL, qty=order["qty"], side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
              client_order_id=f"{PREFIX}{order['strategy']}-{day}", stop_loss=StopLossRequest(stop_price=order["stop"]))
    if "target" in order:
        kw.update(order_class=OrderClass.BRACKET, take_profit=TakeProfitRequest(limit_price=order["target"]))
    else:
        kw.update(order_class=OrderClass.OTO)
    return tc.submit_order(MarketOrderRequest(**kw))


# ---------------------------------------------------------------- commands
def tick(now=None, tc=None, fetch=None, session_lookup=None):
    from aether_tactical import engulfing_engine, orb_engine
    from aether_tactical.bars import fetch_session
    from aether_tactical.market_time import now_ny, session_for
    now = now or now_ny()
    s = (session_lookup or session_for)(now.date())
    if s is None or not (s.open <= now < s.close - timedelta(minutes=5)):
        return "outside session"
    bars = (fetch or fetch_session)(SYMBOL, s, now)
    if not bars:
        return "no bars yet"
    state = load_state()
    day = state.setdefault(s.day, {})
    out = []
    for eng in (orb_engine, engulfing_engine):
        name = eng.NAME
        if name in day:
            continue                                   # one trade (or one decision) per strategy per day
        r = eng.evaluate(bars, s)
        if name == "engulf15" and r.get("state") == "signal":
            r = {**r, "stop": _engulf_stop(bars, s)}
        order, why = plan_entry(r, bars[-1].t, now, bars[-1].c)
        if r.get("state") in ("traded", "no_trade") and order is None:
            day[name] = {"status": "missed" if r.get("state") == "traded" else "no_trade", "reason": r.get("reason")}
            _log("decide", day[name]["status"], r.get("reason", ""), strategy=name)
            continue
        if order is None:
            out.append(f"{name}: {why}")
            if why.startswith("signal is stale"):
                day[name] = {"status": "missed", "reason": why}
                _log("decide", "missed", why, strategy=name)
            continue
        tc = tc or client()
        try:
            o = submit(tc, order, s.day)
            day[name] = {"status": "entered", "order_id": str(o.id), **order}
            _log("order", "submitted", f"BUY {order['qty']} {SYMBOL} stop {order['stop']}"
                 + (f" target {order['target']}" if "target" in order else ""), strategy=name, order_id=str(o.id))
            _tell(f"{name}: BUY {order['qty']} {SYMBOL} (~${order['est_price']:.2f}), stop {order['stop']}"
                  + (f", target {order['target']}" if "target" in order else ", exit by 15:55 NY") + f". {r.get('reason', '')}")
            out.append(f"{name}: entered {order['qty']}")
        except Exception as e:
            day[name] = {"status": "error", "reason": f"{type(e).__name__}: {str(e)[:200]}"}
            _log("order", "error", day[name]["reason"], strategy=name)
            _tell(f"{name}: order FAILED: {day[name]['reason']}")
    save_state(state)
    return "; ".join(out) or "nothing to do"


def _engulf_stop(bars, s):
    from aether_tactical.engulfing_engine import bars15
    return bars15(bars, s)[-1].l


def close(tc=None, now=None):
    from aether_tactical.market_time import now_ny
    tc = tc or client()
    now = now or now_ny()
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
    opn = tc.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[SYMBOL], nested=True))
    n = 0
    for o in opn:
        tc.cancel_order_by_id(o.id)
        n += 1
    try:
        pos = tc.get_open_position(SYMBOL)
        qty = float(pos.qty)
    except Exception:
        qty = 0.0
    msg = f"cancelled {n} open {SYMBOL} order(s)"
    if qty > 0:
        tc.close_position(SYMBOL)
        msg += f"; closing {qty:g} {SYMBOL} at market"
    _log("close", "flat" if qty == 0 else "closing", msg)
    summary = day_summary(tc, now.date().isoformat())
    _tell(msg + ("\n" + summary if summary else ""))
    return msg


def day_summary(tc, day: str) -> str:
    st = load_state().get(day, {})
    lines = [f"{k}: {v.get('status')}" + (f" ({v.get('reason')})" if v.get("reason") else "") for k, v in st.items()]
    return "\n".join(lines)


def status():
    tc = client()
    acct = tc.get_account()
    try:
        p = tc.get_open_position(SYMBOL)
        pos = f"{p.qty} {SYMBOL} @ {p.avg_entry_price}, unrealised {p.unrealized_pl}"
    except Exception:
        pos = f"no {SYMBOL} position"
    from aether_tactical.market_time import now_ny
    print(json.dumps({"account": "stress-test (paper)", "equity": str(acct.equity), "position": pos,
                      "today": load_state().get(now_ny().date().isoformat(), {})}, indent=2, default=str))


def main(argv=None) -> int:
    _load_env()
    cmd = (argv if argv is not None else sys.argv[1:] or ["status"])[0]
    if cmd in ("tick", "close") and os.getenv("TACTICAL_PAPER_EXEC", "0") != "1":
        print("TACTICAL_PAPER_EXEC is not 1: off")
        return 0
    if cmd == "tick":
        print(tick())
    elif cmd == "close":
        print(close())
    elif cmd == "status":
        status()
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
