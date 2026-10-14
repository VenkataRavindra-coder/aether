"""AETHER Risk Controller v2: one gate for every order path.

Called by AlpacaExecutor before any broker submission (crypto strategy,
TradingView webhook, US engine). Closing orders are always allowed so
positions can be reduced. Every decision is logged.

Manual kill switch:  touch ~/AETHER_HALT   (resume: rm ~/AETHER_HALT)

Rules for NEW entries, in order:
  1. manual halt file present                         -> block everything
  2. crypto with real money                           -> block (crypto live is disabled)
  3. engine kill switch active, or not yet evaluated  -> block that engine (fail closed)
  4. unknown / non-positive order size                -> block
  5. real money: order > RISK_MAX_ORDER_USD_LIVE      -> block   (default $100)
  6. real money: today's buys + order > RISK_MAX_DAILY_BUY_USD_LIVE -> block (default $100)
Paper orders are never capped by 5-6, so the $5,000 paper allocation is unaffected.

The "canary" engine key (canary.py, micro-live phase 1) is the second key allowed to spend real
money. It has no daily-loss kill switch of its own (it never holds more than one tiny position),
and its caps are separate from the US engine's so a canary can never starve a rebalance:
  5c. real money: order > min(CANARY_MAX_ORDER_USD, RISK_MAX_ORDER_USD_LIVE) -> block  (default $15)
  6c. real money: today's canary buys + order > CANARY_MAX_DAILY_BUY_USD     -> block  (default $30)
Canary buys are recorded in their own state file (AETHER_CANARY_RISK_STATE) and never consume
RISK_MAX_DAILY_BUY_USD_LIVE. All caps are notional USD.

Real-money entries are also refused while a safety_guard live breaker is latched in the ledger
(rule 3b), so preflight() refuses a whole live rotation on a latch even if the halt file was removed.

Exits are never blocked, including by rule 1: a halt must not trap you in a position.

But a halt must not silently LIQUIDATE one either. A plan that rotates positions (sells,
then re-buys in the same run) is all-or-nothing: gate the whole plan on halted() and call
preflight() over every order before submitting any of them. Otherwise the sells fill and the
buys are refused, leaving the account out of its intended exposure while the dashboard
reports "HALTED: all new entries blocked".
"""
import json
import logging
import math
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("risk_controller")
import safety_guard

# One resolver for the whole system (safety_guard.halt_file). Kept as a module attribute for
# existing callers/tests; canary.py preflight asserts it still equals safety_guard.halt_file().
HALT_FILE = safety_guard.halt_file()

# Engines whose entries need an evaluated daily-loss kill switch.
# None = not evaluated since start-up -> entries blocked until the engine reports.
# "us" is registered as well: it is the only engine that can spend real money, so its
# entries stay blocked until it has reported a kill-switch state at least once.
_KILL = {"crypto": None, "us": None}
# Engine keys allowed to call check()/preflight(). Anything else is refused (entries only;
# closing orders stay allowed, rule 0.7). "stress" is the paper-only stress-test key.
_VALID_ENGINES = frozenset({"crypto", "us", "stress", "canary"})
# Engines allowed to place real-money entries. Crypto live is disabled; stress is paper-only.
_LIVE_ENGINES = frozenset({"us", "canary"})
_LOCK = threading.Lock()


def halted() -> bool:
    return HALT_FILE.exists()


def set_kill(engine: str, active: bool) -> None:
    """Called by an engine every cycle with its daily-loss kill-switch state."""
    with _LOCK:
        _KILL[engine] = bool(active)


def _env_usd(name: str, default: str) -> float:
    """Dollar limit from .env. Malformed or <=0 returns 0, which blocks every entry."""
    raw = os.getenv(name, "").strip() or default
    try:
        v = float(raw)
    except ValueError:
        return 0.0
    return v if v > 0 and math.isfinite(v) else 0.0


def _state_path(engine: str = "us") -> Path:
    if engine == "canary":
        return Path(os.getenv("AETHER_CANARY_RISK_STATE",
                              str(Path.home() / ".aether_canary_risk_state.json")))
    return Path(os.getenv("AETHER_RISK_STATE", str(Path.home() / ".aether_risk_state.json")))


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _live_buys_today(engine: str = "us") -> float:
    try:
        s = json.loads(_state_path(engine).read_text())
    except FileNotFoundError:
        return 0.0
    except Exception:
        return math.inf  # unreadable state fails closed
    return float(s.get("usd", 0.0)) if s.get("day") == _today() else 0.0


def _record_live_buy(usd: float, engine: str = "us") -> None:
    p = _state_path(engine)
    total = _live_buys_today(engine) + usd
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({"day": _today(), "usd": round(total, 2)}))
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def release_live_buy(usd: float, engine: str = "us") -> None:
    """Returns budget that check() reserved but was never actually spent: the order was
    rejected by the broker after the risk check passed, or it filled for less than the
    requested notional. Without this, a failed or partial live buy keeps counting against
    RISK_MAX_DAILY_BUY_USD_LIVE for the rest of the day even though the money was never spent.
    Never goes negative; a bad release amount just floors at the already-recorded total."""
    try:
        usd = float(usd)
    except (TypeError, ValueError):
        return
    if not math.isfinite(usd) or usd <= 0:
        return
    with _LOCK:
        total = max(0.0, _live_buys_today(engine) - usd)
        p = _state_path(engine)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps({"day": _today(), "usd": round(total, 2)}))
        os.chmod(tmp, 0o600)
        tmp.replace(p)


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


def _decide(engine, side, notional, is_closing, real_money, record=True, spent_extra=0.0):
    if side not in ("BUY", "SELL"):
        return False, "invalid_side"
    if is_closing:
        return True, "closing_always_allowed"
    if halted():
        return False, "halted"
    if engine not in _VALID_ENGINES:
        return False, "unknown_engine"
    if real_money and engine == "crypto":
        return False, "crypto_live_disabled"
    if real_money and engine not in _LIVE_ENGINES:
        return False, "live_engine_not_allowed"
    if real_money and safety_guard.breaker_latched():
        # safety_guard would refuse this entry at submit time anyway. Refusing it HERE matters for
        # plans: preflight() runs this rule over every order, so a latched breaker refuses the whole
        # rotation instead of letting the sells fill and the buys bounce off the guard.
        return False, "live_breaker_latched"
    if engine in _KILL:
        state = _KILL[engine]
        if state is None:
            return False, "kill_switch_not_evaluated"
        if state:
            return False, "daily_loss_kill"
    try:
        usd = float(notional)
    except (TypeError, ValueError):
        return False, "unknown_notional"
    if not math.isfinite(usd) or usd <= 0:
        return False, "unknown_notional"
    if real_money:
        cap = _env_usd("RISK_MAX_ORDER_USD_LIVE", "100")
        if engine == "canary":
            cap = min(cap, _env_usd("CANARY_MAX_ORDER_USD", "15"))
            budget = _env_usd("CANARY_MAX_DAILY_BUY_USD", "30")
            cap_name, budget_name = "over_canary_max_order_usd", "over_canary_daily_budget"
        else:
            budget = _env_usd("RISK_MAX_DAILY_BUY_USD_LIVE", "100")
            cap_name, budget_name = "over_max_order_usd_live", "over_daily_live_budget"
        if usd > cap:
            return False, f"{cap_name}({cap:g})"
        # spent_extra counts the earlier buys in a plan being pre-flighted, so a plan whose
        # buys only breach the daily budget in aggregate is still refused as a whole.
        spent = _live_buys_today(engine) + float(spent_extra or 0.0)
        if spent + usd > budget + 1e-9:
            return False, f"{budget_name}(spent {spent:g} of {budget:g})"
        if record:
            _record_live_buy(usd, engine)
    return True, "ok"


def check(engine: str, symbol: str, side: str, notional, is_closing: bool, real_money: bool,
          record: bool = True, spent_extra: float = 0.0):
    """Returns (allowed: bool, reason: str).

    record=False evaluates the rules WITHOUT consuming the real-money daily buy budget.
    spent_extra adds notional already committed earlier in the same plan. Both are for
    preflight(); the actual submission must record."""
    side = (side or "").upper()
    with _LOCK:
        allowed, reason = _decide(engine, side, notional, is_closing, real_money, record=record,
                                  spent_extra=spent_extra)
    _log(engine, symbol, side, notional, real_money, allowed, reason)
    if not allowed:
        logger.warning(f"RISK BLOCK {engine} {side} {symbol}: {reason}")
    return allowed, reason


def preflight(engine: str, orders):
    """Evaluate a WHOLE plan before any order is submitted, without recording spend.

    `orders` is an iterable of (side, notional, is_closing, real_money) tuples.
    Returns (allowed: bool, reason: str) for the first order that would be blocked.

    Why this exists: submitting a plan one order at a time lets the sells fill while the buys
    are refused -- by a halt appearing mid-plan, a per-order cap, or the daily budget. The
    account ends up liquidated and out of its intended exposure. Pre-flighting makes the plan
    decision all-or-nothing. Real-money buys are accumulated across the plan so a plan that
    breaches the daily budget only in aggregate is refused too. The daily buy budget is NOT
    consumed here; only a real submission records it, so a refused plan costs nothing and can
    be retried.
    """
    extra = 0.0
    with _LOCK:
        for side, notional, is_closing, real_money in orders:
            allowed, reason = _decide(engine, (side or "").upper(), notional, is_closing,
                                      real_money, record=False, spent_extra=extra)
            if not allowed:
                return False, reason
            if real_money and not is_closing:
                try:
                    extra += float(notional)
                except (TypeError, ValueError):
                    pass
    return True, "ok"
