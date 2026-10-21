"""Alpaca daily-bar fetch and integrity checks. This module never submits orders."""
from __future__ import annotations

import math
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from .config import Settings

logger = logging.getLogger("AETHER_MACRO_DATA")
NY = ZoneInfo("America/New_York")
UTC = timezone.utc
DATA_BASE_URL = "https://data.alpaca.markets"

class MarketDataError(RuntimeError):
    pass

@dataclass(frozen=True)
class DailyBars:
    symbol: str
    dates: tuple[date, ...]
    closes: tuple[float, ...]
    completed_session: date
    fetched_at: datetime
    mode: str
    feed: str
    adjustment: str = "all"

    @property
    def close(self) -> float:
        return self.closes[-1]

    def sma(self, period: int) -> float:
        return sum(self.closes[-period:]) / period


class AlpacaReadOnlyData:
    """Read-only broker data client with explicit HTTP timeouts and a fixed Alpaca host."""
    def __init__(self, settings: Settings, http=requests):
        self.settings = settings
        self.http = http
        self.base = settings.trading_base_url
        self.headers = {
            "APCA-API-KEY-ID": settings.api_key,
            "APCA-API-SECRET-KEY": settings.api_secret,
            "accept": "application/json",
        }

    def _get(self, path: str, params=None, *, base_url: str | None = None):
        try:
            response = self.http.get(
                (base_url or self.base) + path, headers=self.headers, params=params,
                timeout=(min(3.05, self.settings.timeout_seconds), self.settings.timeout_seconds),
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, (dict, list)):
                raise MarketDataError("Alpaca returned an unexpected response")
            return payload
        except requests.Timeout as exc:
            raise MarketDataError("Alpaca request timed out") from exc
        except requests.RequestException as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code:
                raise MarketDataError(f"Alpaca request failed with HTTP {status_code}") from exc
            raise MarketDataError(f"Alpaca connection failed ({type(exc).__name__})") from exc
        except (ValueError, TypeError) as exc:
            raise MarketDataError("Alpaca returned invalid JSON") from exc

    def _clock(self) -> tuple[datetime, bool]:
        payload = self._get("/v2/clock")
        if not isinstance(payload, dict):
            raise MarketDataError("Alpaca clock response was malformed")
        try:
            stamp = datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError
            return stamp.astimezone(UTC), bool(payload["is_open"])
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            raise MarketDataError("Alpaca clock response was malformed") from exc

    def _calendar(self, start: date, end: date) -> list[tuple[date, datetime]]:
        payload = self._get("/v2/calendar", {"start": start.isoformat(), "end": end.isoformat()})
        if not isinstance(payload, list):
            raise MarketDataError("Alpaca calendar response was malformed")
        sessions = []
        try:
            for row in payload:
                day = date.fromisoformat(row["date"])
                close_time = time.fromisoformat(row["close"])
                close_at = datetime.combine(day, close_time, NY).astimezone(UTC)
                sessions.append((day, close_at))
        except (KeyError, TypeError, ValueError) as exc:
            raise MarketDataError("Alpaca calendar contained a malformed session") from exc
        if sessions != sorted(sessions) or len({d for d, _ in sessions}) != len(sessions):
            raise MarketDataError("Alpaca calendar sessions were duplicated or out of order")
        return sessions

    def _bars(self, symbol: str, start: date, end: datetime) -> list[dict]:
        params = {
            "symbols": symbol,
            "timeframe": "1Day",
            "start": datetime.combine(start, time.min, NY).astimezone(UTC).isoformat(),
            "end": end.isoformat(),
            "adjustment": "all",
            "feed": self.settings.feed,
            "sort": "asc",
            "limit": 1000,
        }
        rows, token = [], None
        # 420 calendar days should fit in one page. Pagination is still handled and capped.
        for _ in range(10):
            p = dict(params)
            if token:
                p["page_token"] = token
            payload = self._get("/v2/stocks/bars", p, base_url=DATA_BASE_URL)
            if not isinstance(payload, dict) or not isinstance(payload.get("bars"), list):
                raise MarketDataError("Alpaca bars response was malformed")
            rows.extend(payload["bars"])
            token = payload.get("next_page_token")
            if not token:
                return rows
        raise MarketDataError("Alpaca returned too many bar pages; refusing partial history")

    @staticmethod
    def _bar_date(row: dict) -> date:
        try:
            stamp = datetime.fromisoformat(row["t"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            return stamp.astimezone(NY).date()
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            raise MarketDataError("Alpaca returned a bar with an invalid timestamp") from exc

    def fetch_verified_daily(self, symbol: str = "SPY") -> DailyBars:
        symbol = symbol.strip().upper()
        if symbol != "SPY":
            raise MarketDataError("This macro gate is registered for SPY only")
        now, market_open = self._clock()
        today_ny = now.astimezone(NY).date()
        start = today_ny - timedelta(days=self.settings.lookback_calendar_days)
        sessions = self._calendar(start, today_ny)
        completed = [(d, c) for d, c in sessions if c <= now]
        if not completed:
            raise MarketDataError("No completed US market session is available")
        expected_last = completed[-1][0]
        bars = self._bars(symbol, start, now)
        by_day = {}
        for row in bars:
            d = self._bar_date(row)
            try:
                close = float(row["c"])
            except (KeyError, TypeError, ValueError) as exc:
                raise MarketDataError("Alpaca returned a bar with an invalid close") from exc
            if not math.isfinite(close) or close <= 0:
                raise MarketDataError("Alpaca returned a non-positive or non-finite close")
            if d > expected_last:
                continue  # never use a same-day partial bar, even if the market clock says open
            if d in by_day:
                raise MarketDataError(f"Alpaca returned duplicate daily bars for {d}")
            by_day[d] = close
        expected_days = [d for d, close_at in sessions if close_at <= now]
        missing = [d for d in expected_days if d not in by_day]
        if missing:
            raise MarketDataError(f"Daily history is missing completed session {missing[-1]}")
        if not by_day or max(by_day) != expected_last:
            raise MarketDataError("Latest SPY bar is stale or does not match the last completed session")
        dates = tuple(sorted(by_day))
        closes = tuple(by_day[d] for d in dates)
        if len(closes) < 200:
            raise MarketDataError(f"Only {len(closes)} completed daily bars; need at least 200")
        if market_open and dates[-1] >= today_ny:
            raise MarketDataError("Refusing to use a daily bar from the currently open session")
        return DailyBars(symbol, dates, closes, expected_last, now, self.settings.mode, self.settings.feed)
