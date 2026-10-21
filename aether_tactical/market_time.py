"""Session times in New York, from Alpaca's market calendar. Fails closed: no calendar, no session."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Session:
    day: str            # YYYY-MM-DD
    open: datetime      # aware, New York
    close: datetime     # aware, New York (13:00 on early-close days)


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=NY) if dt.tzinfo is None else dt.astimezone(NY)


def trading_client():
    """Read-only use: calendar only. Paper keys (the calendar is the same for every account)."""
    from alpaca.trading.client import TradingClient
    k, s = os.getenv("APCA_API_KEY_ID", ""), os.getenv("APCA_API_SECRET_KEY", "")
    if not k or not s:
        raise RuntimeError("missing APCA_API_KEY_ID / APCA_API_SECRET_KEY")
    return TradingClient(k, s, paper=True)


def session_for(day: date, client=None) -> Session | None:
    """The regular session on `day`, or None if the market is closed that day."""
    from alpaca.trading.requests import GetCalendarRequest
    cal = (client or trading_client()).get_calendar(GetCalendarRequest(start=day, end=day))
    for c in cal or []:
        if c.date == day:
            return Session(day.isoformat(), _aware(c.open), _aware(c.close))
    return None


def now_ny() -> datetime:
    return datetime.now(NY)
