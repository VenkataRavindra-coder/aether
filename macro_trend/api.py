"""FastAPI read-only signal route. The route never accesses order-submission APIs."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict
from typing import Callable

from fastapi import APIRouter, HTTPException, Path

from .config import Settings
from .data import AlpacaReadOnlyData, DailyBars, MarketDataError
from .strategy import evaluate_trend

logger = logging.getLogger("AETHER_MACRO_API")

class SignalService:
    def __init__(self, fetch: Callable[[str], DailyBars], cache_seconds: int = 60):
        self.fetch = fetch
        self.cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._cached: dict[str, tuple[float, dict]] = {}

    def signal(self, symbol: str) -> dict:
        symbol = symbol.strip().upper()
        now = time.monotonic()
        with self._lock:
            hit = self._cached.get(symbol)
            if hit and now - hit[0] < self.cache_seconds:
                payload = dict(hit[1])
                payload["cached"] = True
                payload["cache_age_seconds"] = round(now - hit[0], 2)
                return payload
            try:
                bars = self.fetch(symbol)
                payload = asdict(evaluate_trend(bars))
                payload["status"] = "available"
                payload["recommended_action"] = ("BUY_OR_HOLD_LONG" if payload["signal"] == "RISK_ON"
                                                 else "DEFENSIVE_CASH_OR_EXIT")
                payload["cached"] = False
                payload["cache_age_seconds"] = 0.0
            except Exception as exc:
                # Do not leak response bodies, credentials, or request headers to callers.
                logger.error("Macro signal unavailable for %s (%s)", symbol, type(exc).__name__)
                raise MarketDataError("Signal unavailable; Alpaca data could not be verified") from exc
            self._cached[symbol] = (now, payload)
            return dict(payload)


def build_router(service: SignalService | None = None) -> APIRouter:
    router = APIRouter(tags=["macro trend"])
    if service is None:
        def fetch(symbol: str) -> DailyBars:
            settings = Settings.from_env()
            return AlpacaReadOnlyData(settings).fetch_verified_daily(symbol)
        service = SignalService(fetch, cache_seconds=_cache_seconds_from_env())

    @router.get("/signal/{symbol}", summary="Read-only SPY 200-day macro trend signal")
    def get_signal(symbol: str = Path(min_length=1, max_length=8, pattern=r"^[A-Za-z.]+$")):
        if symbol.upper() != "SPY":
            raise HTTPException(status_code=404, detail={"status": "unsupported", "symbol": symbol.upper(),
                                                          "signal": "NO_SIGNAL"})
        try:
            return service.signal(symbol)
        except MarketDataError as exc:
            raise HTTPException(status_code=503, detail={"status": "unavailable", "symbol": symbol.upper(),
                                                          "signal": "NO_SIGNAL", "recommended_action": "NO_ACTION",
                                                          "reason": str(exc)}) from exc
    return router



def _cache_seconds_from_env() -> int:
    import os
    try:
        value = int(os.getenv("MACRO_SIGNAL_CACHE_SECONDS", "60"))
    except ValueError:
        logger.error("Invalid MACRO_SIGNAL_CACHE_SECONDS; disabling cache")
        return 0
    if not 0 <= value <= 3600:
        logger.error("MACRO_SIGNAL_CACHE_SECONDS out of range; disabling cache")
        return 0
    return value
