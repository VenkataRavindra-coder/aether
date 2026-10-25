"""Research page: GET /research (page) and GET /api/research (JSON). Read-only.

Shows every registered strategy with its historical, forward (shadow), paper and live evidence kept apart,
the evidence grade (strongest evidence first, not highest return), forward-journal health and the trial count.
Everything comes from files via research_registry.build(); it never imports an executor or calls a broker.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

import research_registry as rr

PAGE = rr.REPO / "research.html"


def build_router() -> APIRouter:
    r = APIRouter()

    @r.get("/api/research")
    def api():
        try:
            body = rr.build()
        except Exception as e:  # the page must say so rather than show stale or invented numbers
            body = {"ok": False, "errors": [f"{type(e).__name__}: {str(e)[:200]}"]}
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    @r.get("/research", response_class=HTMLResponse)
    def page():
        return HTMLResponse(PAGE.read_text(), headers={"Cache-Control": "no-store"})

    return r
