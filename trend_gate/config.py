"""Trend Gate settings, read once from environment variables.

Data mode only selects WHICH Alpaca keys fetch market data. Market data is the same for paper and
live keys, so the default is paper: the live (real-money) keys are never needed to compute a signal.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


KEY_NAMES = {
    "paper": ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY"),
    "live": ("APCA_LIVE_API_KEY_ID", "APCA_LIVE_API_SECRET_KEY"),
}


@dataclass(frozen=True)
class Settings:
    mode: str = "paper"                      # TREND_GATE_MODE: paper | live (which keys read data)
    symbols: tuple = ("SPY",)                # TREND_GATE_SYMBOLS: allow-list served by the API
    sma_window: int = 200                    # TREND_GATE_SMA_WINDOW: trading days
    lookback_days: int = 700                 # calendar days fetched: 200 sessions for the SMA + ~1 year to chart
    band_pct: float = 0.0                    # TREND_GATE_BAND_PCT: optional no-trade band around the SMA
    feed: str = "sip"                        # TREND_GATE_FEED: sip (consolidated) | iex; sip falls back to iex
    adjustment: str = "split"                # TREND_GATE_ADJUSTMENT: split | all (all also adjusts dividends)
    fetch_timeout_s: float = 10.0            # TREND_GATE_TIMEOUT_S: hard cap per broker call
    cache_ttl_s: int = 300                   # TREND_GATE_CACHE_TTL_S: reuse a verified signal this long
    max_stale_s: int = 6 * 3600              # TREND_GATE_MAX_STALE_S: serve last good (flagged stale) this long
    max_bar_age_days: int = 5                # newest completed bar may be at most this many calendar days old
    max_daily_move_pct: float = 25.0         # a bigger one-day close change is treated as bad data
    key_names: tuple = field(default=KEY_NAMES["paper"])

    @classmethod
    def from_env(cls) -> "Settings":
        mode = os.getenv("TREND_GATE_MODE", "paper").strip().lower()
        if mode not in KEY_NAMES:
            mode = "paper"
        symbols = tuple(s.strip().upper() for s in os.getenv("TREND_GATE_SYMBOLS", "SPY").split(",") if s.strip())
        feed = os.getenv("TREND_GATE_FEED", "sip").strip().lower()
        adj = os.getenv("TREND_GATE_ADJUSTMENT", "split").strip().lower()
        return cls(
            mode=mode,
            symbols=symbols or ("SPY",),
            sma_window=max(2, _int("TREND_GATE_SMA_WINDOW", 200)),
            band_pct=max(0.0, _float("TREND_GATE_BAND_PCT", 0.0)),
            feed=feed if feed in ("sip", "iex") else "sip",
            adjustment=adj if adj in ("split", "all") else "split",
            fetch_timeout_s=max(1.0, _float("TREND_GATE_TIMEOUT_S", 10.0)),
            cache_ttl_s=max(0, _int("TREND_GATE_CACHE_TTL_S", 300)),
            max_stale_s=max(0, _int("TREND_GATE_MAX_STALE_S", 6 * 3600)),
            key_names=KEY_NAMES[mode],
        )

    def credentials(self) -> tuple[str, str]:
        """(key, secret) for the selected mode. Raises if missing: no signal without real data."""
        k, s = (os.getenv(n, "").strip() for n in self.key_names)
        if not k or not s:
            raise RuntimeError(f"missing Alpaca data keys {self.key_names[0]} / {self.key_names[1]}")
        return k, s
