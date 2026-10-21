"""Environment-backed, read-only settings for the macro trend signal."""
from __future__ import annotations

import os
from dataclasses import dataclass

class ConfigurationError(ValueError):
    pass

@dataclass(frozen=True)
class Settings:
    mode: str
    api_key: str
    api_secret: str
    feed: str = "iex"
    timeout_seconds: float = 8.0
    lookback_calendar_days: int = 420
    cache_seconds: int = 60

    @property
    def trading_base_url(self) -> str:
        # Fixed allowlist: credentials are never sent to a caller-supplied URL.
        return "https://paper-api.alpaca.markets" if self.mode == "paper" else "https://api.alpaca.markets"

    @classmethod
    def from_env(cls, environ=None) -> "Settings":
        e = os.environ if environ is None else environ
        mode = e.get("ALPACA_MODE", "paper").strip().lower()
        if mode not in {"paper", "live"}:
            raise ConfigurationError("ALPACA_MODE must be either 'paper' or 'live'")
        key_name, secret_name = (("APCA_API_KEY_ID", "APCA_API_SECRET_KEY") if mode == "paper"
                                 else ("APCA_LIVE_API_KEY_ID", "APCA_LIVE_API_SECRET_KEY"))
        key, secret = e.get(key_name, "").strip(), e.get(secret_name, "").strip()
        if not key or not secret:
            raise ConfigurationError(f"credentials for ALPACA_MODE={mode} are not configured")
        feed = e.get("ALPACA_DATA_FEED", "iex").strip().lower()
        if feed not in {"iex", "sip"}:
            raise ConfigurationError("ALPACA_DATA_FEED must be iex or sip")
        try:
            timeout = float(e.get("ALPACA_HTTP_TIMEOUT_SECONDS", "8"))
            lookback = int(e.get("SPY_SMA_LOOKBACK_CALENDAR_DAYS", "420"))
            cache = int(e.get("MACRO_SIGNAL_CACHE_SECONDS", "60"))
        except (TypeError, ValueError) as exc:
            raise ConfigurationError("timeout, lookback and cache settings must be numeric") from exc
        if not 1 <= timeout <= 30:
            raise ConfigurationError("ALPACA_HTTP_TIMEOUT_SECONDS must be between 1 and 30")
        if lookback < 300:
            raise ConfigurationError("SPY_SMA_LOOKBACK_CALENDAR_DAYS must be at least 300")
        if not 0 <= cache <= 3600:
            raise ConfigurationError("MACRO_SIGNAL_CACHE_SECONDS must be between 0 and 3600")
        return cls(mode, key, secret, feed, timeout, lookback, cache)
