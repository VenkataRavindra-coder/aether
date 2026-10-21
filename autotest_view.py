"""Auto test page: GET /autotest (page) and GET /api/autotest (JSON). Read-only.

Shows the automatic live test (strategy.md 10, "Automated round trips") from FILES only:
- the status snapshot the test job writes after every plan/run/close
  (tests/evidence/canary/auto-status.json),
- the job's rows in the decision log,
- whether the switch is on in .env, whether the halt file exists, and the two timers' next runs.
It never imports the test-trading code and never calls the broker, so the engine stays isolated
from the live order path (tests/test_canary.py).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

REPO = Path(__file__).resolve().parent
PAGE = REPO / "autotest.html"
STATUS = REPO / "tests" / "evidence" / "canary" / "auto-status.json"
ENGINES = ("canary", "canary_auto")


def _switch_on(env_file: Path = REPO / ".env") -> bool | None:
    """Reads .env directly: the job reads it fresh on every run, so the engine's own environment
    (loaded at its start) could be stale."""
    try:
        val = None
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("CANARY_AUTO="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'").lower()
        return val in ("1", "true", "yes", "on")
    except FileNotFoundError:
        return None


def _status() -> dict | None:
    try:
        return json.loads(STATUS.read_text())
    except FileNotFoundError:
        return None


def _log_rows(limit: int = 30) -> list[dict]:
    import decision_log
    con = sqlite3.connect(f"file:{decision_log.DB_PATH}?mode=ro", uri=True, timeout=2)
    try:
        q = ",".join("?" * len(ENGINES))
        rows = con.execute(f"SELECT ts, engine, event, symbol, decision, reason FROM decisions WHERE engine IN ({q}) "
                           "ORDER BY id DESC LIMIT ?", (*ENGINES, limit)).fetchall()
    finally:
        con.close()
    return [dict(zip(("ts", "engine", "event", "symbol", "decision", "reason"), r)) for r in rows]


def _timers() -> dict:
    import map_status
    jobs = map_status.units_view()["jobs"]
    return {k: jobs.get(k) for k in ("canary-run", "canary-close")}


def build() -> dict:
    out = {"ok": True}
    for key, fn in (("switch_on", _switch_on), ("status", _status), ("log", _log_rows), ("timers", _timers)):
        try:
            out[key] = fn()
        except Exception as e:
            out[key] = None
            out.setdefault("errors", []).append(f"{key}: {type(e).__name__}: {str(e)[:160]}")
    try:
        import safety_guard
        out["halt"] = safety_guard.halt_file().exists()
    except Exception as e:
        out["halt"] = None
        out.setdefault("errors", []).append(f"halt: {type(e).__name__}")
    return out


def build_router() -> APIRouter:
    r = APIRouter()

    @r.get("/api/autotest")
    def api():
        return JSONResponse(build(), headers={"Cache-Control": "no-store"})

    @r.get("/autotest", response_class=HTMLResponse)
    def page():
        return HTMLResponse(PAGE.read_text(), headers={"Cache-Control": "no-store"})

    return r
