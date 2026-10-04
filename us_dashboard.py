"""AETHER US-only dashboard: read-only views + one entries-only control (pause).

Mount in the existing FastAPI app (keeps session login + Cloudflare Access):
    from us_dashboard import build_router
    app.include_router(build_router(<your session-login dependency>))

Routes: GET /us (page) · GET /api/us/summary (JSON) · POST /api/us/pause
There is deliberately NO resume route: resume = ssh aether; rm ~/AETHER_HALT
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

import safety_guard as g
from heartbeat import heartbeat_file

HTML_PATH = Path(__file__).with_name("us_dashboard.html")
CSRF_TOKEN = secrets.token_urlsafe(32)        # per process, embedded in the served page
CACHE_TTL = 10
_cache: dict = {}

ACCOUNTS = {
    # name: (base url, env var naming the KEY var, default KEY var, same pair for SECRET)
    "paper": ("https://paper-api.alpaca.markets",
              "DASH_PAPER_KEY_VAR", "APCA_API_KEY_ID", "DASH_PAPER_SECRET_VAR", "APCA_API_SECRET_KEY"),
    "live": ("https://api.alpaca.markets",
             "DASH_LIVE_KEY_VAR", "APCA_LIVE_API_KEY_ID", "DASH_LIVE_SECRET_VAR", "APCA_LIVE_API_SECRET_KEY"),
}


# ---------- data access (read-only) ---------------------------------------
def alpaca_get(acct: str, path: str):
    base, kv, kd, sv, sd = ACCOUNTS[acct]
    key = os.environ.get(os.environ.get(kv, kd))
    sec = os.environ.get(os.environ.get(sv, sd))
    if not key or not sec:
        raise RuntimeError(f"{acct} API keys not configured")
    req = urllib.request.Request(base + path, headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _cached(key: str, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    try:
        val = fn()
    except Exception as e:  # never 500 the page; the card shows the error
        val = {"error": f"{type(e).__name__}: {e}"}
    _cache[key] = (now, val)
    return val


def _get_path(obj, dotted: str):
    for part in [p for p in (dotted or "").split(".") if p]:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj


def load_stops() -> dict | None:
    """Engine-managed stops. US_STOPS_FILE = engine JSON state file, US_STOPS_KEY =
    dotted path to {symbol: stop} or {symbol: {"stop": x}}. None = not connected."""
    path = os.environ.get("US_STOPS_FILE")
    if not path or not Path(path).exists():
        return None
    data = _get_path(json.loads(Path(path).read_text()), os.environ.get("US_STOPS_KEY", ""))
    if not isinstance(data, dict):
        return None
    out = {}
    for sym, v in data.items():
        v = v.get("stop") if isinstance(v, dict) else v
        try:
            out[str(sym).upper()] = float(v)
        except (TypeError, ValueError):
            pass
    return out


def account_view(acct: str, stops: dict | None) -> dict:
    a = alpaca_get(acct, "/v2/account")
    pos = alpaca_get(acct, "/v2/positions")
    eq = float(a["equity"])
    last = float(a.get("last_equity") or eq)
    rows = []
    for p in pos:
        if p.get("asset_class", "us_equity") != "us_equity":
            continue  # US-only dashboard: crypto positions excluded
        sym = p["symbol"]
        price = float(p["current_price"])
        stop = None if stops is None else stops.get(sym)
        rows.append({
            "symbol": sym, "qty": float(p["qty"]), "avg": float(p["avg_entry_price"]),
            "price": price, "upl": float(p["unrealized_pl"]),
            "upl_today": float(p.get("unrealized_intraday_pl") or 0),
            "stop": stop,
            "stop_gap": (price - stop) / price if stop else None,
            "stop_state": "not_connected" if stops is None else ("set" if stop else "missing"),
        })
    return {"equity": eq, "cash": float(a["cash"]), "account_day_pl": eq - last,
            "us_open_day_pl": sum(r["upl_today"] for r in rows),
            "status": a.get("status"), "positions": rows}


def risk_view(live_equity: float | None) -> dict:
    lim = g.limits()
    L = g.load_ledger()
    last = L.get("last_mark")
    hf = g.halt_file()
    out = {
        "limits": lim.__dict__,
        "halted": hf.exists(),
        "halt_reason": hf.read_text().strip()[:300] if hf.exists() else None,
        "initialised": bool(last),
    }
    if not last:
        return out
    today = datetime.now(g.ET).date()
    ref = L["prev_close"] if last["date"] == today.isoformat() else last["equity"]
    st = L.get("last_status") or {}
    out.update({
        "level": st.get("level"),
        "latched": L.get("latched"),
        "last_mark": last,
        "mark_age_days": (today - datetime.fromisoformat(last["date"]).date()).days,
        "daily_loss": g._loss(ref, live_equity) if live_equity else None,
        "weekly_loss": st.get("weekly_loss"),
        "monthly_loss": st.get("monthly_loss"),
        "drawdown": st.get("drawdown"),
        "peak": L.get("peak"),
    })
    return out


def decisions_view() -> dict:
    """Decision log, opened read-only. DECISION_DB = sqlite path; DECISION_SQL = query
    returning columns named ts, symbol, action, gate, reason (alias in SQL as needed)."""
    db = os.environ.get("DECISION_DB")
    if not db or not Path(db).exists():
        return {"connected": False, "rows": [], "note": "DECISION_DB not set or missing"}
    sql = os.environ.get("DECISION_SQL",
                         "SELECT ts, symbol, action, gate, reason FROM decisions ORDER BY ts DESC LIMIT 25")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
    try:
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(sql).fetchall()]
    finally:
        con.close()
    keep = ("ts", "symbol", "action", "gate", "reason")
    return {"connected": True, "rows": [{k: r.get(k) for k in keep} for r in rows]}


def health_view() -> dict:
    hb_path = heartbeat_file()
    hb = json.loads(hb_path.read_text()) if hb_path.exists() else {}
    ts = (hb.get("engine") or {}).get("ts")
    wd_path = g.home() / "watchdog_state.json"
    wd = json.loads(wd_path.read_text()) if wd_path.exists() else None
    stale_min = float(os.environ.get("WATCHDOG_STALE_MIN", 15))
    age = time.time() - ts if ts else None
    return {
        "heartbeat_age_sec": age,
        "engine_ok": age is not None and age <= stale_min * 60,
        "watchdog": None if wd is None else (wd.get("key") or "ok"),
        "crypto_strategy": os.environ.get("STRATEGY_ENABLED", "unknown"),
        "live_trading_flag": os.environ.get("LIVE_TRADING", "unknown"),
    }


def build_summary() -> dict:
    stops = _cached("stops", load_stops)
    stops = None if isinstance(stops, dict) and "error" in stops else stops
    live = _cached("live", lambda: account_view("live", stops))
    paper = _cached("paper", lambda: account_view("paper", stops))
    clock = _cached("clock", lambda: alpaca_get("paper", "/v2/clock"))
    live_eq = live.get("equity") if "error" not in live else None
    health = health_view()
    risk = risk_view(live_eq)
    if risk["halted"]:
        state = "paused"
    elif risk.get("latched"):
        state = "latched"
    elif not health["engine_ok"]:
        state = "engine_down"
    else:
        state = "running"
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "state": state,
        "market_open": clock.get("is_open"),
        "us_allocation": float(os.environ.get("US_ALLOCATION_USD", 5000)),
        "live": live, "paper": paper, "risk": risk, "health": health,
        "signals": _cached("signals", decisions_view),
    }


# ---------- routes -----------------------------------------------------------
def build_router(auth_dep: Callable) -> APIRouter:
    r = APIRouter(dependencies=[Depends(auth_dep)])

    @r.get("/us", response_class=HTMLResponse)
    def page():
        return HTML_PATH.read_text().replace("__CSRF__", CSRF_TOKEN)

    @r.get("/api/us/summary")
    def summary():
        return build_summary()

    @r.post("/api/us/pause")
    def pause(request: Request):
        if not secrets.compare_digest(request.headers.get("x-aether-csrf", ""), CSRF_TOKEN):
            raise HTTPException(status_code=403, detail="bad csrf token")
        hf = g.halt_file()
        if not hf.exists():
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            g.atomic_write(hf, f"paused from dashboard at {stamp}\n")
        _cache.clear()
        return {"halted": True}

    return r
