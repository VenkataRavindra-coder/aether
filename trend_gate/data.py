"""Daily bars from Alpaca, fetched with a hard timeout and verified before any signal uses them.

Fail closed: if the data is missing, short, stale, out of order or implausible, DataError is raised
and the caller reports "no signal" instead of guessing.
"""
from __future__ import annotations

import logging
import math
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import Settings

log = logging.getLogger("aether.trend_gate")
NY = ZoneInfo("America/New_York")
# Small dedicated pool: a hung broker call occupies one worker and times out for the caller,
# instead of blocking the web server's own threads.
_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="trend-gate-fetch")


class DataError(Exception):
    """The bars cannot be trusted for a signal (the message says why)."""


class FetchTimeout(DataError):
    """The broker did not answer within the configured timeout."""


@dataclass(frozen=True)
class Bar:
    date: str      # session date in New York, YYYY-MM-DD
    close: float


def completed_bars(bars: list[Bar], now: datetime) -> list[Bar]:
    """Drop today's bar while the session is still running (before 16:15 New York): it is partial."""
    now_ny = now.astimezone(NY)
    today = now_ny.date().isoformat()
    if bars and bars[-1].date == today and (now_ny.hour, now_ny.minute) < (16, 15):
        return bars[:-1]
    return bars


def verify_bars(symbol: str, bars: list[Bar], settings: Settings, now: datetime) -> list[Bar]:
    """Return the bars if they are fit for a signal, else raise DataError."""
    w = settings.sma_window
    if len(bars) < w:
        raise DataError(f"{symbol}: only {len(bars)} completed daily bars, need {w}")
    dates = [b.date for b in bars]
    if any(a >= b for a, b in zip(dates, dates[1:])):
        raise DataError(f"{symbol}: bar dates are not strictly increasing (duplicate or out-of-order bars)")
    for b in bars[-w:]:
        if not (isinstance(b.close, float) and math.isfinite(b.close) and b.close > 0):
            raise DataError(f"{symbol}: invalid close {b.close!r} on {b.date}")
    age = (now.astimezone(NY).date() - date.fromisoformat(dates[-1])).days
    if age > settings.max_bar_age_days:
        raise DataError(f"{symbol}: newest completed bar is {dates[-1]} ({age} days old); data is stale")
    window = bars[-w:]
    for a, b in zip(window, window[1:]):
        move = abs(b.close / a.close - 1) * 100
        if move > settings.max_daily_move_pct:
            raise DataError(f"{symbol}: {move:.1f}% close-to-close move on {b.date} exceeds "
                            f"{settings.max_daily_move_pct:g}%; treating as bad data")
    return bars


class AlpacaDailyBars:
    """Fetches split-adjusted (or fully adjusted) daily closes. `client` is injectable for tests."""

    def __init__(self, settings: Settings, client=None):
        self.settings = settings
        self._client = client

    def _get_client(self):
        if self._client is None:
            from alpaca.data.historical import StockHistoricalDataClient
            key, secret = self.settings.credentials()
            self._client = StockHistoricalDataClient(key, secret)
        return self._client

    def _request(self, symbol: str, now: datetime, feed: str):
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        # The free data plan serves consolidated (SIP) history only up to 15 minutes ago.
        end = now - timedelta(minutes=16)
        return StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
            start=end - timedelta(days=self.settings.lookback_days), end=end,
            adjustment=Adjustment.SPLIT if self.settings.adjustment == "split" else Adjustment.ALL,
            feed=DataFeed.SIP if feed == "sip" else DataFeed.IEX)

    def _fetch_raw(self, symbol: str, now: datetime) -> list[Bar]:
        client = self._get_client()
        feeds = [self.settings.feed] + (["iex"] if self.settings.feed == "sip" else [])
        last_err = None
        for feed in feeds:
            try:
                data = client.get_stock_bars(self._request(symbol, now, feed)).data
                rows = data.get(symbol, []) if hasattr(data, "get") else []
                return [Bar(b.timestamp.astimezone(NY).date().isoformat(), float(b.close)) for b in rows]
            except Exception as e:   # e.g. 403 "subscription does not permit querying recent SIP data"
                last_err = e
                if feed == "sip" and len(feeds) > 1:
                    log.warning("trend_gate: SIP bars failed for %s (%s); retrying with IEX", symbol, str(e)[:160])
        raise DataError(f"{symbol}: broker error: {type(last_err).__name__}: {str(last_err)[:200]}")

    def fetch(self, symbol: str, now: datetime | None = None) -> list[Bar]:
        """Completed daily bars, oldest first. Raises FetchTimeout / DataError; never hangs the caller."""
        now = now or datetime.now(timezone.utc)
        fut = _POOL.submit(self._fetch_raw, symbol, now)
        try:
            bars = fut.result(timeout=self.settings.fetch_timeout_s)
        except FutureTimeout:
            fut.cancel()
            raise FetchTimeout(f"{symbol}: Alpaca did not answer within {self.settings.fetch_timeout_s:g}s")
        return completed_bars(bars, now)
