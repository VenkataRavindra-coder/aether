"""Live system map: GET /map (page) and GET /api/map/status (JSON).

One read-only snapshot of the whole setup for the animated map page (map_view.html): engine threads,
heartbeat, order gate (halt file, kill switches, live budgets, breakers), systemd jobs (last and next
run, result), accounts, backups, daily-check evidence, stress result and recent decisions.

Read-only by design: it reads files, runs `systemctl show`, and sends GET /v2/account to Alpaca
(through dashboard_v2, same as the main dashboard). It never writes, never places or cancels orders,
never touches the halt file and never returns secrets. Every section fails soft
({"ok": false, "error": ...}) so the page shows "unknown" instead of a guess.

Auth: the _dashboard_auth middleware in aether_engine.py covers these paths (default-deny), so the
router takes a no-op auth dependency like us_extras does.
"""
from __future__ import annotations

import glob
import json
import os
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

REPO = Path(__file__).resolve().parent
PAGE = REPO / "map_view.html"
CACHE_TTL = 5
ACCOUNT_TTL = 30
_STARTED = time.time()
_cache: dict = {"t": 0.0, "v": None}
_acct_cache: dict = {"t": 0.0, "v": None}
_lock = threading.Lock()

JOBS = ("health", "watchdog", "backup", "daily", "stress", "riskmark")
THREADS = {"aether-us": "us", "aether-strategy": "crypto", "aether-monitor": "monitor"}
_ENV = {k: v for k, v in os.environ.items() if k != "NOTIFY_SOCKET"}  # see aether_engine._SUBPROCESS_ENV


def _section(fn, *a) -> dict:
    try:
        return {"ok": True, **fn(*a)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _iso(ts: float | None):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None


def _num(x):
    """JSON-safe number: inf/nan (risk_controller's fail-closed values) become None."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


# ---------------------------------------------------------------- sections
def engine_view(info: Callable[[], dict]) -> dict:
    alive = {t.name for t in threading.enumerate()}
    out = {"uptime_s": round(time.time() - _STARTED),
           "threads": {key: name in alive for name, key in THREADS.items()},
           "flags": {k: _flag(k, d) for k, d in (("US_ENABLED", "false"), ("US_EXECUTE", "false"),
                                                  ("US_LIVE", "false"), ("LIVE_TRADING", "false"),
                                                  ("STRATEGY_ENABLED", "false"), ("US_REQUIRE_APPROVAL", "true"))}}
    out.update(info() or {})
    return out


def heartbeat_view() -> dict:
    from heartbeat import heartbeat_file
    hb = json.loads(heartbeat_file().read_text())
    ts = (hb.get("engine") or {}).get("ts")
    return {"ts": _iso(ts), "age_s": round(time.time() - ts) if ts else None}


def gate_view() -> dict:
    import risk_controller as rc
    import safety_guard as sg
    ledger = {}
    try:
        L = sg.load_ledger()
        latched = L.get("latched") or None
        ledger = {"initialised": bool(L.get("last_mark")),
                  "last_mark": (L.get("last_mark") or {}).get("date"),
                  "level": (L.get("last_status") or {}).get("level"),
                  "latched": bool(latched), "latched_reasons": (latched or {}).get("reasons", [])}
    except FileNotFoundError:
        ledger = {"initialised": False}
    return {
        "halt": sg.halt_file().exists(),
        "kill": dict(rc._KILL),
        "live_us": {"spent": _num(rc._live_buys_today("us")),
                    "cap_order": rc._env_usd("RISK_MAX_ORDER_USD_LIVE", "100"),
                    "cap_day": rc._env_usd("RISK_MAX_DAILY_BUY_USD_LIVE", "100")},
        "live_canary": {"spent": _num(rc._live_buys_today("canary")),
                        "cap_order": min(rc._env_usd("RISK_MAX_ORDER_USD_LIVE", "100"),
                                         rc._env_usd("CANARY_MAX_ORDER_USD", "15")),
                        "cap_day": rc._env_usd("CANARY_MAX_DAILY_BUY_USD", "30")},
        "ledger": ledger,
    }


def _unit_names() -> list[str]:
    engine = os.environ.get("AETHER_SERVICE", "aether.service").strip() or "aether.service"
    units = [engine, "cloudflared.service"]
    for j in JOBS:
        units += [f"aether-{j}.timer", f"aether-{j}.service"]
    return units


def _usec(v: str):
    v = (v or "").strip()
    if v.startswith("@"):
        v = v[1:]
    try:
        n = float(v)
    except ValueError:
        return None
    return n if n > 0 else None


def parse_show(text: str) -> dict:
    """Parses `systemctl show --timestamp=unix -p ... u1 u2 ...` (blocks separated by blank lines)."""
    units, cur = {}, {}
    for line in text.splitlines() + [""]:
        if not line.strip():
            if cur.get("Id"):
                units[cur["Id"]] = cur
            cur = {}
            continue
        k, _, v = line.partition("=")
        cur[k] = v
    return units


def units_view(run=subprocess.run) -> dict:
    names = _unit_names()
    r = run(["systemctl", "show", "--timestamp=unix",
             "-p", "Id,LoadState,ActiveState,SubState,Result,LastTriggerUSec,NextElapseUSecRealtime,"
                   "ActiveEnterTimestamp,ExecMainStartTimestamp,ExecMainExitTimestamp", *names],
            capture_output=True, text=True, timeout=10, env=_ENV)
    if r.returncode != 0 and not r.stdout:
        raise RuntimeError((r.stderr or "systemctl failed").strip()[:150])
    u = parse_show(r.stdout)

    def brief(name):
        x = u.get(name) or {}
        return {"unit": name, "loaded": x.get("LoadState") == "loaded", "active": x.get("ActiveState"),
                "sub": x.get("SubState"), "result": x.get("Result")}

    engine = brief(names[0])
    engine["since"] = _iso(_usec((u.get(names[0]) or {}).get("ActiveEnterTimestamp")))
    jobs = {}
    for j in JOBS:
        t, s = u.get(f"aether-{j}.timer") or {}, u.get(f"aether-{j}.service") or {}
        last = _usec(t.get("LastTriggerUSec")) or _usec(s.get("ExecMainStartTimestamp"))
        jobs[j] = {"timer_loaded": t.get("LoadState") == "loaded", "timer_active": t.get("ActiveState") == "active",
                   "last_run": _iso(last), "next_run": _iso(_usec(t.get("NextElapseUSecRealtime"))),
                   "running": s.get("ActiveState") in ("activating", "active") and s.get("SubState") != "exited",
                   "result": s.get("Result")}
    return {"engine": engine, "tunnel": brief("cloudflared.service"), "jobs": jobs}


def _accounts() -> dict:
    now = time.time()
    if _acct_cache["v"] is not None and now - _acct_cache["t"] < ACCOUNT_TTL:
        return _acct_cache["v"]
    import dashboard_v2 as dv
    v = {}
    for mode in ("paper", "live"):
        try:
            v[mode] = {"ok": True, **dv._account_summary(mode)}
        except Exception as e:
            v[mode] = {"ok": False, "error": type(e).__name__}
    _acct_cache.update(t=now, v=v)
    return v


def accounts_view() -> dict:
    return _accounts()


def backups_view(home: Path = REPO) -> dict:
    files = sorted(glob.glob(str(home / "backups" / "aether-*.tar.gz")), key=os.path.getmtime)
    if not files:
        return {"last": None, "age_h": None, "count": 0}
    m = os.path.getmtime(files[-1])
    return {"last": _iso(m), "age_h": round((time.time() - m) / 3600, 1), "count": len(files),
            "size_kb": round(os.path.getsize(files[-1]) / 1024),
            "offsite_target": "storagebox" if os.getenv("BACKUP_STORAGEBOX") else
                              ("azure" if os.getenv("BACKUP_SAS_URL") else _icloud_target(home, files[-1])),
            "offsite_age_h": _icloud_age(home)}


def _receipt(home: Path):
    try:
        name, ts, _w = (home / ".offsite_receipt").read_text().split()[:3]
        return name, (time.time() - float(ts)) / 3600
    except Exception:
        return None


def _icloud_target(home: Path, newest: str):
    """'iCloud (Mac)' only when the Mac confirmed holding the NEWEST archive; otherwise report it as behind."""
    r = _receipt(home)
    if not r:
        return None
    return "iCloud (via Mac)" if r[0] == os.path.basename(newest) else f"iCloud is behind ({round(r[1])} h)"


def _icloud_age(home: Path):
    r = _receipt(home)
    return None if not r else round(r[1], 1)


def daily_view(home: Path = REPO) -> dict:
    files = sorted(glob.glob(str(home / "tests" / "evidence" / "daily" / "*.json")), key=os.path.getmtime)
    if not files:
        return {"utc": None, "checks": []}
    body = json.loads(Path(files[-1]).read_text())
    return {"utc": body.get("utc"), "all_green": body.get("all_green"),
            "checks": [{"id": r.get("id"), "ok": r.get("ok"), "detail": str(r.get("detail", ""))[:160]}
                       for r in body.get("results", [])]}


def stress_view(home: Path = REPO) -> dict:
    s = json.loads((home / ".stress_last_run.json").read_text())
    return {"rc": s.get("rc"), "utc": s.get("utc")}


def activity_view(limit: int = 15) -> dict:
    import decision_log
    con = sqlite3.connect(f"file:{decision_log.DB_PATH}?mode=ro", uri=True, timeout=2)
    try:
        rows = con.execute("SELECT id, ts, engine, mode, symbol, event, decision, reason FROM decisions "
                           "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        con.close()
    keys = ("id", "ts", "engine", "mode", "symbol", "event", "decision", "reason")
    return {"rows": [{k: (str(v)[:160] if isinstance(v, str) else v) for k, v in zip(keys, r)} for r in rows]}


def server_view() -> dict:
    import us_extras
    return us_extras.server_view()


def research_view() -> dict:
    import us_extras
    r = us_extras.research_view()
    return {k: r.get(k) for k in ("trials_total", "dsr", "dsr_gate", "pbo_gate")}


def build_status(info: Callable[[], dict]) -> dict:
    now = time.time()
    with _lock:
        if _cache["v"] is not None and now - _cache["t"] < CACHE_TTL:
            return _cache["v"]
        v = {"generated_at": _iso(now), "engine": _section(engine_view, info), "heartbeat": _section(heartbeat_view),
             "gate": _section(gate_view), "units": _section(units_view), "accounts": _section(accounts_view),
             "backups": _section(backups_view), "daily": _section(daily_view), "stress": _section(stress_view),
             "activity": _section(activity_view), "server": _section(server_view), "research": _section(research_view)}
        _cache.update(t=now, v=v)
        return v


def build_router(auth_dep: Callable, engine_info: Callable[[], dict] = lambda: {}) -> APIRouter:
    r = APIRouter(dependencies=[Depends(auth_dep)])

    @r.get("/map", response_class=HTMLResponse)
    def map_page():
        return HTMLResponse(PAGE.read_text(), headers={"Cache-Control": "no-store"})

    @r.get("/api/map/status")
    def map_status():
        return build_status(engine_info)

    return r
