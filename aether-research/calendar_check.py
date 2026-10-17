"""Second-source check of the US market calendar (build list: clock/calendar gate, bad-data second source).

The engine trusts ONE calendar: Alpaca's clock (aether_engine.us_market_state). That stays the primary.
This script compares Alpaca's calendar with an independent one (pandas_market_calendars, NYSE) for the
coming weeks and reports every day where they disagree: a holiday one side doesn't know, or a different
open/close time (half days: day after Thanksgiving, Christmas Eve, July 3rd).

Read-only. It never touches the engine, the Risk Controller or any order path, and needs only the PAPER
keys (calendar data is the same on both accounts).

    python calendar_check.py               # next 60 days from today
    python calendar_check.py --days 120 --notify

Exit codes: 0 sources agree; 1 they disagree (do not trust the calendar for those days until resolved);
2 a source could not be read (fail closed: treat as disagreement, not as "fine").
"""
import argparse
import os
import sys
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
NORMAL_OPEN, NORMAL_CLOSE = time(9, 30), time(16, 0)


def compare(primary, second):
    """Both args: {date: (open_time_ET, close_time_ET)} for trading days only. Pure; no I/O.
    Returns a sorted list of human-readable mismatches (empty list = agree)."""
    issues = []
    for d in sorted(set(primary) | set(second)):
        a, b = primary.get(d), second.get(d)
        if a is None:
            issues.append(f"{d}: Alpaca says CLOSED, NYSE calendar says open {b[0]:%H:%M}-{b[1]:%H:%M}")
        elif b is None:
            issues.append(f"{d}: Alpaca says open {a[0]:%H:%M}-{a[1]:%H:%M}, NYSE calendar says CLOSED")
        elif a != b:
            issues.append(f"{d}: Alpaca {a[0]:%H:%M}-{a[1]:%H:%M} vs NYSE calendar {b[0]:%H:%M}-{b[1]:%H:%M}")
    return issues


def special_days(cal):
    """Days in `cal` that are not a normal 09:30-16:00 session (half days). For the report."""
    return {d: v for d, v in cal.items() if v != (NORMAL_OPEN, NORMAL_CLOSE)}


def nyse_calendar(start: date, end: date):
    import pandas_market_calendars as mcal  # research-only dependency (requirements-dev.txt)
    sched = mcal.get_calendar("NYSE").schedule(start_date=start.isoformat(), end_date=end.isoformat())
    out = {}
    for _, row in sched.iterrows():
        o = row["market_open"].tz_convert(ET)
        c = row["market_close"].tz_convert(ET)
        out[o.date()] = (o.time().replace(second=0, microsecond=0), c.time().replace(second=0, microsecond=0))
    return out


def _as_time(v):
    if isinstance(v, datetime):
        v = (v.astimezone(ET) if v.tzinfo else v).time()
    elif isinstance(v, str):
        v = time.fromisoformat(v if len(v) > 5 else v + ":00")
    return v.replace(second=0, microsecond=0)


def alpaca_calendar(start: date, end: date):
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetCalendarRequest
    key, sec = os.getenv("APCA_API_KEY_ID", ""), os.getenv("APCA_API_SECRET_KEY", "")
    if not key or not sec:
        raise RuntimeError("APCA_API_KEY_ID / APCA_API_SECRET_KEY (paper) not set")
    days = TradingClient(key, sec, paper=True).get_calendar(GetCalendarRequest(start=start, end=end))
    out = {}
    for c in days:
        d = c.date if isinstance(c.date, date) else date.fromisoformat(str(c.date)[:10])
        out[d] = (_as_time(c.open), _as_time(c.close))
    return out


def run(days: int, notify_on_issue: bool = False, today: date = None) -> int:
    start = today or datetime.now(ET).date()
    end = start + timedelta(days=days)
    try:
        primary = alpaca_calendar(start, end)
        second = nyse_calendar(start, end)
    except Exception as e:  # fail closed
        msg = f"Calendar check could not read a source ({type(e).__name__}: {e}). Treat as UNVERIFIED."
        print(msg)
        _maybe_notify(notify_on_issue, msg)
        return 2
    issues = compare(primary, second)
    print(f"Calendar check {start}..{end}: {len(primary)} Alpaca sessions, {len(second)} NYSE sessions")
    for d, (o, c) in sorted(special_days(primary).items()):
        print(f"  special session (Alpaca): {d} {o:%H:%M}-{c:%H:%M}")
    if not issues:
        print("AGREE")
        return 0
    print("DISAGREE:")
    for line in issues:
        print("  " + line)
    _maybe_notify(notify_on_issue, "Calendar sources disagree:\n" + "\n".join(issues))
    return 1


def _maybe_notify(on: bool, text: str):
    if not on:
        return
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import notify
        notify.send_sync("AETHER " + text)
    except Exception as e:
        print(f"(notify failed: {e})")


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    except ImportError:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--notify", action="store_true", help="Telegram alert on disagreement or a failed read")
    a = ap.parse_args()
    raise SystemExit(run(a.days, a.notify))
