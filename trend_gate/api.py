"""FastAPI control plane: GET /signal/{symbol}.

200 -> a verified signal (check "stale"); 503 -> no trustworthy signal right now; 404 -> symbol not served.
Also GET /signal/{symbol}/history (close + SMA series for the chart) and GET /trend (the dashboard page).
Inside AETHER the router is mounted on the engine (aether_engine.py), behind its login middleware.
Standalone (local only):  uvicorn trend_gate.api:app --host 127.0.0.1 --port 8010
"""
from __future__ import annotations

from fastapi import APIRouter, FastAPI, HTTPException
from pathlib import Path

from fastapi.responses import HTMLResponse, JSONResponse

from .service import SignalService

PAGE = Path(__file__).resolve().parent.parent / "trend.html"
_service: SignalService | None = None


def service() -> SignalService:
    global _service
    if _service is None:          # lazy: importing this module never needs keys or network
        _service = SignalService()
    return _service


def build_router() -> APIRouter:
    r = APIRouter()

    @r.get("/signal/{symbol}")
    def signal(symbol: str):
        try:
            out = service().get(symbol)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"{symbol.upper()} is not a Trend Gate symbol")
        return JSONResponse(out, status_code=200 if out.get("ok") else 503,
                            headers={"Cache-Control": "no-store"})

    @r.get("/signal/{symbol}/history")
    def history(symbol: str, days: int = 252):
        try:
            out = service().history(symbol, days)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"{symbol.upper()} is not a Trend Gate symbol")
        return JSONResponse(out, status_code=200 if out.get("ok") else 503, headers={"Cache-Control": "no-store"})

    @r.get("/trend", response_class=HTMLResponse)
    def trend_page():
        """The Trend Gate page of the AETHER dashboard (linked from the board)."""
        return HTMLResponse(PAGE.read_text(), headers={"Cache-Control": "no-store"})

    return r


def create_app() -> FastAPI:
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    a = FastAPI(title="AETHER Trend Gate", docs_url=None, redoc_url=None)
    a.include_router(build_router())
    return a


app = create_app()
