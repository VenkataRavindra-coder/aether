"""Read-only tactical endpoints, mounted on the engine behind its login middleware.

GET /tactical/signal/{symbol}  today's live state of both strategies (paper; no orders)
GET /tactical/ledger           ledger summary, verdicts and the last 20 scored rows
GET /tactical                  the dashboard page
"""
from __future__ import annotations

import threading
import time

from fastapi import APIRouter, HTTPException
from pathlib import Path

from fastapi.responses import HTMLResponse, JSONResponse

from . import engulfing_engine, ledger, orb_engine
from .bars import BarError, fetch_session
from .market_time import now_ny, session_for

SYMBOLS = ("SPY",)
PAGE = Path(__file__).resolve().parent.parent / "tactical.html"
_cache: dict = {}
_lock = threading.Lock()
TTL_S = 30


def live_state(symbol: str) -> dict:
    now = now_ny()
    s = session_for(now.date())
    if s is None:
        return {"ok": True, "symbol": symbol, "market": "closed today", "strategies": []}
    if now < s.open:
        return {"ok": True, "symbol": symbol, "market": f"opens {s.open:%H:%M} New York", "strategies": []}
    bars = fetch_session(symbol, s, now)
    return {"ok": True, "symbol": symbol, "market": "open" if now < s.close else "closed",
            "session": {"open": s.open.isoformat(), "close": s.close.isoformat()},
            "last_bar": bars[-1].t.strftime("%H:%M") if bars else None, "bars": len(bars),
            "prices": [[b.t.strftime("%H:%M"), round(b.c, 4)] for b in bars],
            "strategies": [e.evaluate(bars, s) for e in (orb_engine, engulfing_engine)] if bars else [],
            "as_of": now.isoformat(timespec="seconds"), "paper_only": True}


def build_router() -> APIRouter:
    r = APIRouter()

    @r.get("/tactical/signal/{symbol}")
    def signal(symbol: str):
        sym = symbol.upper()
        if sym not in SYMBOLS:
            raise HTTPException(status_code=404, detail=f"{sym} is not a tactical symbol")
        with _lock:
            hit = _cache.get(sym)
            if hit and time.time() - hit[0] < TTL_S:
                return JSONResponse(hit[1], headers={"Cache-Control": "no-store"})
            try:
                out = live_state(sym)
                _cache[sym] = (time.time(), out)
                return JSONResponse(out, headers={"Cache-Control": "no-store"})
            except (BarError, Exception) as e:
                return JSONResponse({"ok": False, "symbol": sym, "error": f"{type(e).__name__}: {str(e)[:300]}"},
                                    status_code=503, headers={"Cache-Control": "no-store"})

    @r.get("/tactical/ledger")
    def ledger_view():
        try:
            return JSONResponse({"ok": True, "summary": ledger.summary(), "recent": ledger.rows()[-20:][::-1],
                                 "paper_only": True}, headers={"Cache-Control": "no-store"})
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}, status_code=503)

    @r.get("/tactical", response_class=HTMLResponse)
    def tactical_page():
        """The Tactical page of the AETHER dashboard (linked from the board and Trend Gate pages)."""
        return HTMLResponse(PAGE.read_text(), headers={"Cache-Control": "no-store"})

    return r
