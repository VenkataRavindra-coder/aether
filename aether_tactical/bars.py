"""IEX 1-minute bars for one regular session, verified. Fail closed with BarError."""
from __future__ import annotations

import math
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import datetime, timedelta

from .market_time import NY, Session

_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tactical-fetch")
TIMEOUT_S = float(os.getenv("TACTICAL_TIMEOUT_S", "20"))


class BarError(Exception):
    pass


@dataclass(frozen=True)
class Bar:
    t: datetime      # bar START time, aware New York
    o: float
    h: float
    l: float
    c: float
    v: float


def regular_hours(bars: list[Bar], s: Session) -> list[Bar]:
    """Only bars that start inside the regular session (drops pre-market and after-hours)."""
    return [b for b in bars if s.open <= b.t < s.close]


def verify(bars: list[Bar], s: Session) -> list[Bar]:
    if not bars:
        raise BarError(f"{s.day}: no regular-hours bars")
    ts = [b.t for b in bars]
    if any(a >= b for a, b in zip(ts, ts[1:])):
        raise BarError(f"{s.day}: bar times not strictly increasing")
    for b in bars:
        vals = (b.o, b.h, b.l, b.c)
        if not all(math.isfinite(x) and x > 0 for x in vals) or b.l > min(b.o, b.c) or b.h < max(b.o, b.c):
            raise BarError(f"{s.day}: malformed bar at {b.t:%H:%M}")
    for a, b in zip(bars, bars[1:]):
        if abs(b.o / a.c - 1) > 0.05:
            raise BarError(f"{s.day}: {abs(b.o / a.c - 1) * 100:.1f}% jump at {b.t:%H:%M}; treating as bad data")
    return bars


def fetch_session(symbol: str, s: Session, now: datetime | None = None, client=None) -> list[Bar]:
    """Regular-hours IEX bars of session `s` up to `now` (only fully closed minutes), verified."""
    now = (now or datetime.now(NY)).astimezone(NY)
    end = min(s.close, now.replace(second=0, microsecond=0))   # the current minute is still forming

    def _get():
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        c = client
        if c is None:
            from alpaca.data.historical import StockHistoricalDataClient
            c = StockHistoricalDataClient(os.getenv("APCA_API_KEY_ID"), os.getenv("APCA_API_SECRET_KEY"))
        req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Minute,
                               start=s.open, end=end, feed=DataFeed.IEX)
        rows = c.get_stock_bars(req).data.get(symbol, [])
        return [Bar(r.timestamp.astimezone(NY), float(r.open), float(r.high), float(r.low), float(r.close),
                    float(r.volume)) for r in rows]

    if end <= s.open:
        return []
    fut = _POOL.submit(_get)
    try:
        raw = fut.result(timeout=TIMEOUT_S)
    except FutureTimeout:
        raise BarError(f"{s.day}: Alpaca did not answer within {TIMEOUT_S:g}s")
    except Exception as e:
        raise BarError(f"{s.day}: broker error: {type(e).__name__}: {str(e)[:200]}")
    closed = [b for b in regular_hours(raw, s) if b.t + timedelta(minutes=1) <= end]
    return verify(closed, s) if closed else []
