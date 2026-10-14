"""AETHER live safety guard (strategy.md -> "Risk Governance").

Pure, offline module (no network). Responsibilities:
  * live-only risk ledger (fed ONLY by risk_mark.py from the live Alpaca account)
  * daily / weekly / monthly loss breakers and peak-to-trough drawdown breaker
  * order gate: exits ALWAYS pass; entries pass only if every rule allows
  * broker minimum-notional rule: skip, never round up

All thresholds are fractions (0.02 = 2%) read from env at call time.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


# ---------- paths ----------------------------------------------------------
def home() -> Path:
    return Path(os.environ.get("AETHER_HOME", str(Path.home())))


def halt_file() -> Path:
    """THE halt-file path. Every component (risk_controller, safety_guard, risk_mark, both
    dashboards, canary.py) resolves it here, so a halt written by one is seen by all.
    Order: AETHER_HALT_FILE if set, else AETHER_HOME/AETHER_HALT if AETHER_HOME is set,
    else ~/AETHER_HALT. Absolute, user-expanded."""
    explicit = os.environ.get("AETHER_HALT_FILE", "").strip()
    if explicit:
        return Path(explicit).expanduser().absolute()
    return (home() / "AETHER_HALT").expanduser().absolute()


def legacy_halt_file() -> Path:
    """Where safety_guard wrote breaker halts before 2026-10-14 (AETHER_HOME/AETHER_HALT,
    ignoring AETHER_HALT_FILE). A file left here is invisible to everything now."""
    return (home() / "AETHER_HALT").expanduser().absolute()


def breaker_latched() -> bool:
    """True if a live breaker is latched in the ledger. Unreadable ledger -> True (fail closed).
    No ledger yet -> False (the order gate separately blocks live entries until it exists)."""
    try:
        return bool(load_ledger().get("latched"))
    except Exception:
        return True


def ledger_file() -> Path:
    return home() / "live_risk_ledger.json"


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


# ---------- limits ---------------------------------------------------------
def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


@dataclass(frozen=True)
class Limits:
    daily: float
    weekly: float
    monthly: float
    dd_warn: float
    dd_reduce: float
    dd_halt: float
    min_notional: float
    max_mark_age_days: int


def limits() -> Limits:
    cap = _f("GUARD_DD_ABS_CAP", 0.10)
    oos = _f("GUARD_OOS_WORST_DD", 0.0)  # worst OUT-OF-SAMPLE drawdown, fraction
    halt = min(cap, 2 * oos) if oos > 0 else cap
    return Limits(
        daily=_f("GUARD_DAILY_LOSS", 0.02),
        weekly=_f("GUARD_WEEKLY_LOSS", 0.05),
        monthly=_f("GUARD_MONTHLY_LOSS", 0.08),
        dd_warn=0.50 * halt,      # scaled off halt -> always ordered
        dd_reduce=0.75 * halt,
        dd_halt=halt,
        min_notional=_f("GUARD_MIN_NOTIONAL", 1.0),
        max_mark_age_days=int(_f("GUARD_MAX_MARK_AGE_DAYS", 4)),
    )


# ---------- ledger ---------------------------------------------------------
def load_ledger() -> dict:
    p = ledger_file()
    return json.loads(p.read_text()) if p.exists() else {}


def save_ledger(L: dict) -> None:
    atomic_write(ledger_file(), json.dumps(L, indent=2, sort_keys=True))


def week_id(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def month_id(d: date) -> str:
    return d.strftime("%Y-%m")


def _loss(anchor: float, equity: float) -> float:
    return (anchor - equity) / anchor if anchor > 0 else 1.0


def evaluate(L: dict, equity: float, lim: Limits | None = None) -> dict:
    lim = lim or limits()
    weekly = _loss(L["week"]["equity"], equity)
    monthly = _loss(L["month"]["equity"], equity)
    dd = _loss(L["peak"], equity)
    reasons = []
    if weekly >= lim.weekly:
        reasons.append(f"weekly loss {weekly:.2%} >= {lim.weekly:.2%}")
    if monthly >= lim.monthly:
        reasons.append(f"monthly loss {monthly:.2%} >= {lim.monthly:.2%}")
    if dd >= lim.dd_halt:
        reasons.append(f"drawdown {dd:.2%} >= halt {lim.dd_halt:.2%}")
    if reasons:
        level = "HALT"
    elif dd >= lim.dd_reduce:
        level = "REDUCE"
    elif dd >= lim.dd_warn:
        level = "WARN"
    else:
        level = "OK"
    return {"level": level, "reasons": reasons, "equity": equity,
            "weekly_loss": weekly, "monthly_loss": monthly, "drawdown": dd,
            "peak": L["peak"]}


def mark(equity: float, on: date | None = None) -> dict:
    """Record a live-equity mark (call once per trading day at a fixed time).
    Weekly/monthly/drawdown breakers are judged ONLY on these marks."""
    if equity <= 0:
        raise ValueError("live equity must be > 0 to mark the ledger")
    on = on or datetime.now(ET).date()
    L = load_ledger()
    last = L.get("last_mark")
    if not last:
        L = {"prev_close": equity, "peak": equity, "latched": None,
             "week": {"id": week_id(on), "equity": equity},
             "month": {"id": month_id(on), "equity": equity},
             "cash_flows": []}
    else:
        base = last["equity"]
        if last["date"] != on.isoformat():
            L["prev_close"] = base
        if L["week"]["id"] != week_id(on):
            L["week"] = {"id": week_id(on), "equity": base}
        if L["month"]["id"] != month_id(on):
            L["month"] = {"id": month_id(on), "equity": base}
    L["peak"] = max(L["peak"], equity)
    L["last_mark"] = {"date": on.isoformat(), "equity": equity}
    status = evaluate(L, equity)
    if status["level"] == "HALT" and not L.get("latched"):
        L["latched"] = {"at": on.isoformat(), "reasons": status["reasons"]}
        if not halt_file().exists():
            atomic_write(halt_file(), "risk breaker: " + "; ".join(status["reasons"]) + "\n")
    status["latched"] = L.get("latched")
    L["last_status"] = status
    save_ledger(L)
    return status


def record_cash_flow(amount: float, on: date | None = None) -> dict:
    """Deposit (+) / withdrawal (-) so it is not mistaken for P&L."""
    L = load_ledger()
    if not L.get("last_mark"):
        raise RuntimeError("ledger not initialised; run a mark first")
    for k in ("prev_close", "peak"):
        L[k] += amount
    L["week"]["equity"] += amount
    L["month"]["equity"] += amount
    L["last_mark"]["equity"] += amount
    on = on or datetime.now(ET).date()
    L.setdefault("cash_flows", []).append({"date": on.isoformat(), "amount": amount})
    save_ledger(L)
    return L


def reset_after_review() -> dict:
    """Manual reset after review: clears latch and rebases week/month/peak to
    the last marked equity (drawdown is measured fresh from here)."""
    L = load_ledger()
    if not L.get("last_mark"):
        raise RuntimeError("ledger not initialised")
    eq = L["last_mark"]["equity"]
    L["peak"] = eq
    L["week"]["equity"] = eq
    L["month"]["equity"] = eq
    L["latched"] = None
    L["last_status"] = evaluate(L, eq)
    L["last_status"]["latched"] = None
    save_ledger(L)
    return L


# ---------- order gate -----------------------------------------------------
@dataclass(frozen=True)
class Decision:
    allowed: bool
    notional: float
    kind: str      # exit | entry | short | invalid
    reason: str


def check_live_order(side: str, symbol: str, notional: float, position_qty: float,
                     account_equity: float | None, sell_qty: float | None = None,
                     now: datetime | None = None) -> Decision:
    """Gate for LIVE orders. Exits are never blocked by halts or breakers."""
    side = (side or "").lower()
    if side == "sell":
        if position_qty > 0:
            if sell_qty is not None and sell_qty > position_qty + 1e-9:
                return Decision(False, 0.0, "short", f"{symbol}: sell {sell_qty} > held {position_qty} would go short")
            return Decision(True, notional, "exit", f"{symbol}: exit always allowed")
        return Decision(False, 0.0, "short", f"{symbol}: sell with no long position (shorting disabled)")
    if side != "buy":
        return Decision(False, 0.0, "invalid", f"unknown side {side!r}")

    def block(msg: str) -> Decision:
        return Decision(False, 0.0, "entry", f"{symbol}: entry blocked - {msg}")

    lim = limits()
    if halt_file().exists():
        return block("~/AETHER_HALT present")
    L = load_ledger()
    last = L.get("last_mark")
    if not last:
        return block("live ledger not initialised (run risk_admin.py mark)")
    now = now or datetime.now(ET)
    last_date = date.fromisoformat(last["date"])
    if (now.date() - last_date).days > lim.max_mark_age_days:
        return block(f"risk mark stale since {last['date']} (breakers blind)")
    if L.get("latched"):
        return block("breaker latched: " + "; ".join(L["latched"]["reasons"]) + " (manual review + risk_admin.py reset)")
    if account_equity is None or account_equity <= 0:
        return block("live equity unknown or zero")
    ref = L["prev_close"] if last_date == now.date() else last["equity"]
    daily = _loss(ref, account_equity)
    if daily >= lim.daily:
        return block(f"daily loss {daily:.2%} >= {lim.daily:.2%} (resets next trading day)")
    note = ""
    if (L.get("last_status") or {}).get("level") == "REDUCE":
        notional *= 0.5
        note = " (size halved: drawdown reduce level)"
    if notional < lim.min_notional:
        return block(f"notional ${notional:.2f} < broker min ${lim.min_notional:.2f}; skipped, not rounded up{note}")
    return Decision(True, round(notional, 2), "entry", f"{symbol}: entry allowed{note}")
