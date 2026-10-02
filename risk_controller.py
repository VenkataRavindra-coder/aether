"""AETHER Risk Controller v1: one gate for every order path.

Called by AlpacaExecutor before any broker submission (crypto strategy,
TradingView webhook, US engine). Closing orders are always allowed so
positions can be reduced. Every decision is logged.
Manual kill switch:  touch ~/AETHER_HALT  (resume: rm ~/AETHER_HALT)
"""
import logging
import math
import os
from pathlib import Path

logger = logging.getLogger("risk_controller")
HALT_FILE = Path(os.getenv("AETHER_HALT_FILE", str(Path.home() / "AETHER_HALT")))


def halted() -> bool:
    return HALT_FILE.exists()


def _max_order_usd() -> float:
    """RISK_MAX_ORDER_USD: unset = no cap (v1), malformed or <=0 = block everything."""
    raw = os.getenv("RISK_MAX_ORDER_USD", "").strip()
    if not raw:
        return math.inf
    try:
        v = float(raw)
    except ValueError:
        return 0.0
    return v if v > 0 and math.isfinite(v) else 0.0


def _log(engine, symbol, side, notional, real_money, allowed, reason):
    if os.getenv("RISK_LOG", "1") == "0":
        return
    try:
        import decision_log
        decision_log.log(engine, "risk_check",
                         decision="allow" if allowed else "block",
                         mode="live" if real_money else "paper",
                         reason=f"{side} {symbol} ${notional}: {reason}")
    except Exception as e:
        logger.warning(f"risk decision not logged: {e}")


def check(engine: str, symbol: str, side: str, notional, is_closing: bool, real_money: bool):
    """Returns (allowed: bool, reason: str)."""
    side = (side or "").upper()
    if side not in ("BUY", "SELL"):
        allowed, reason = False, "invalid_side"
    elif is_closing:
        allowed, reason = True, "closing_always_allowed"
    elif halted():
        allowed, reason = False, "halted"
    elif real_money and engine == "crypto":
        allowed, reason = False, "crypto_live_disabled"
    elif notional is None or not math.isfinite(float(notional)) or float(notional) <= 0:
        allowed, reason = False, "unknown_notional"
    elif float(notional) > _max_order_usd():
        allowed, reason = False, f"over_max_order_usd({_max_order_usd():g})"
    else:
        allowed, reason = True, "ok"
    _log(engine, symbol, side, notional, real_money, allowed, reason)
    if not allowed:
        logger.warning(f"RISK BLOCK {engine} {side} {symbol}: {reason}")
    return allowed, reason
