"""Fetch -> verify -> evaluate, with a short cache and safe failure.

If the broker fails, the last verified signal is served for up to max_stale_s, clearly flagged
"stale" with its age and the error. After that, the answer is "no signal": never a guess.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from .config import Settings
from .data import AlpacaDailyBars, verify_bars
from .strategy import evaluate

log = logging.getLogger("aether.trend_gate")


class SignalService:
    def __init__(self, settings: Settings | None = None, fetcher=None):
        self.settings = settings or Settings.from_env()
        self.fetcher = fetcher or AlpacaDailyBars(self.settings)
        self._cache: dict[str, tuple[float, dict]] = {}
        self._bars: dict[str, list] = {}   # last VERIFIED bars per symbol, for the chart
        self._lock = threading.Lock()

    def get(self, symbol: str) -> dict:
        symbol = symbol.upper()
        if symbol not in self.settings.symbols:
            raise KeyError(symbol)
        with self._lock:   # one broker call at a time; concurrent requests reuse the result
            hit = self._cache.get(symbol)
            if hit and time.time() - hit[0] < self.settings.cache_ttl_s:
                return {**hit[1], "stale": False}
            try:
                now = datetime.now(timezone.utc)
                bars = verify_bars(symbol, self.fetcher.fetch(symbol, now), self.settings, now)
                prev = hit[1].get("status") if hit else None
                sig = evaluate(symbol, [b.date for b in bars], [b.close for b in bars],
                               self.settings.sma_window, self.settings.band_pct, prev)
                payload = {**sig.to_dict(), "ok": True, "data_mode": self.settings.mode,
                           "computed_at": now.isoformat(timespec="seconds")}
                if prev and prev != sig.status:
                    log.warning("trend_gate: %s flipped %s -> %s (close %.2f, SMA%d %.2f)",
                                symbol, prev, sig.status, sig.close, sig.sma_window, sig.sma)
                else:
                    log.info("trend_gate: %s %s (close %.2f, SMA%d %.2f, %+.2f%%)", symbol, sig.status,
                             sig.close, sig.sma_window, sig.sma, sig.distance_pct)
                self._cache[symbol] = (time.time(), payload)
                self._bars[symbol] = bars
                return {**payload, "stale": False}
            except Exception as e:
                err = f"{type(e).__name__}: {str(e)[:300]}"
                log.error("trend_gate: %s signal failed: %s", symbol, err)
                if hit and time.time() - hit[0] < self.settings.max_stale_s:
                    return {**hit[1], "stale": True, "stale_age_s": round(time.time() - hit[0]), "error": err}
                return {"ok": False, "symbol": symbol, "status": "NO_SIGNAL",
                        "action": "NO_ACTION (data unavailable or failed verification)", "error": err}

    def history(self, symbol: str, days: int = 252) -> dict:
        """Close and 200-day SMA for the last `days` sessions, from the last verified bars only."""
        sig = self.get(symbol)                      # refreshes (or fails) exactly like /signal
        bars = self._bars.get(symbol.upper())
        if not bars:
            return {"ok": False, "symbol": symbol.upper(), "error": sig.get("error", "no verified data yet")}
        w = self.settings.sma_window
        closes = [b.close for b in bars]
        pts, run = [], sum(closes[:w - 1])
        for i in range(w - 1, len(bars)):
            run += closes[i]
            pts.append([bars[i].date, round(closes[i], 4), round(run / w, 4)])
            run -= closes[i - w + 1]
        return {"ok": True, "symbol": symbol.upper(), "sma_window": w, "stale": sig.get("stale", False),
                "points": pts[-max(1, min(days, 1000)):]}
