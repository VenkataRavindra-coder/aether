"""Extra read-only data for the US dashboard: order lifecycle, server health,
research gate, backups. Complements us_dashboard's /api/us/summary.

    GET /api/us/extras    (same auth dependency as the dashboard)

Every section fails soft ({"ok": false, "error": ...}) so the page shows
"unknown" instead of a made-up value. Never returns secrets, never places orders.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Depends

import us_dashboard as usd

REPO = Path(__file__).resolve().parent
CACHE_TTL = 10
_cache: dict = {"t": 0.0, "v": None}


def _section(fn) -> dict:
    try:
        return {"ok": True, **fn()}
    except Exception as e:  # the card shows "unknown", the page never 500s
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}


def _account_name() -> str:
    return "live" if os.environ.get("US_LIVE", "").strip().lower() in ("1", "true", "yes", "on") else "paper"


def _iso(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def orders_view() -> dict:
    acct = _account_name()
    raw = usd.alpaca_get(acct, "/v2/orders?status=all&limit=25&direction=desc")
    held = {p["symbol"] for p in usd.alpaca_get(acct, "/v2/positions")
            if p.get("asset_class", "us_equity") == "us_equity"}
    rows = []
    for o in raw:
        if o.get("asset_class", "us_equity") != "us_equity":
            continue
        sub, fil = _iso(o.get("submitted_at")), _iso(o.get("filled_at"))
        rows.append({
            "symbol": o["symbol"], "side": o["side"], "type": o.get("type"),
            "status": o["status"], "qty": o.get("qty"), "notional": o.get("notional"),
            "limit_price": o.get("limit_price"), "filled_avg_price": o.get("filled_avg_price"),
            "submitted_at": o.get("submitted_at"), "filled_at": o.get("filled_at"),
            "fill_latency_sec": round((fil - sub).total_seconds(), 1) if sub and fil else None,
            "holding": o["side"] == "buy" and o["status"] == "filled" and o["symbol"] in held,
        })
    return {"account": acct, "orders": rows}


def server_view() -> dict:
    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            mem[k] = int(v.split()[0])
    total = mem["MemTotal"]
    avail = mem.get("MemAvailable", mem["MemFree"])
    du = shutil.disk_usage(str(REPO))
    return {
        "load1": round(os.getloadavg()[0], 2), "cpus": os.cpu_count() or 1,
        "mem_pct": round(100 * (total - avail) / total, 1), "mem_total_gb": round(total / 1048576, 1),
        "disk_pct": round(100 * du.used / du.total, 1), "disk_total_gb": round(du.total / 1e9, 1),
    }


def _find_number(obj, key):
    if isinstance(obj, dict):
        if isinstance(obj.get(key), (int, float)):
            return float(obj[key])
        obj = list(obj.values())
    if isinstance(obj, list):
        for v in obj:
            hit = _find_number(v, key)
            if hit is not None:
                return hit
    return None


def research_view() -> dict:
    rdir = REPO / "aether-research"
    con = sqlite3.connect(f"file:{rdir / 'research.sqlite'}?mode=ro", uri=True, timeout=2)
    try:
        logged = con.execute("SELECT COUNT(*) FROM research_trials").fetchone()[0]
        back = con.execute("SELECT COALESCE(SUM(count), 0) FROM untracked_trials").fetchone()[0]
    finally:
        con.close()
    files = sorted(glob.glob(str(rdir / "us_momentum" / "results" / "strategy_truth_*.json")))
    dsr = _find_number(json.loads(Path(files[-1]).read_text()), "dsr") if files else None
    return {"trials_logged": logged, "trials_backfilled": back, "trials_total": logged + back,
            "dsr": dsr, "dsr_gate": 0.95, "pbo_gate": 0.05,
            "evidence_file": Path(files[-1]).name if files else None}


def backups_view() -> dict:
    files = sorted(glob.glob(str(REPO / "backups" / "aether-*.tar.gz")), key=os.path.getmtime)
    if not files:
        return {"last": None, "count": 0, "offsite": bool(os.environ.get("BACKUP_SAS_URL"))}
    f = files[-1]
    return {"last": datetime.fromtimestamp(os.path.getmtime(f), timezone.utc).isoformat(timespec="seconds"),
            "size_kb": round(os.path.getsize(f) / 1024), "count": len(files),
            "offsite": bool(os.environ.get("BACKUP_SAS_URL"))}


def build_extras() -> dict:
    now = time.time()
    if _cache["v"] is not None and now - _cache["t"] < CACHE_TTL:
        return _cache["v"]
    v = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
         "orders": _section(orders_view), "server": _section(server_view),
         "research": _section(research_view), "backups": _section(backups_view)}
    _cache.update(t=now, v=v)
    return v


def build_router(auth_dep: Callable) -> APIRouter:
    r = APIRouter(dependencies=[Depends(auth_dep)])

    @r.get("/api/us/extras")
    def extras():
        return build_extras()

    return r
