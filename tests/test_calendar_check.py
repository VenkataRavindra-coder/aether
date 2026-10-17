"""Tests for aether-research/calendar_check.py (second-source US market calendar). Offline: no broker calls."""
import sys
from datetime import date, time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "aether-research"))
import calendar_check as cc  # noqa: E402

N = (time(9, 30), time(16, 0))
HALF = (time(9, 30), time(13, 0))


def test_agreeing_calendars_have_no_issues():
    cal = {date(2026, 11, 25): N, date(2026, 11, 27): HALF}
    assert cc.compare(cal, dict(cal)) == []


def test_missing_holiday_is_reported_both_ways():
    a = {date(2026, 11, 25): N}
    b = {date(2026, 11, 25): N, date(2026, 11, 26): N}
    assert len(cc.compare(a, b)) == 1 and "Alpaca says CLOSED" in cc.compare(a, b)[0]
    assert "NYSE calendar says CLOSED" in cc.compare(b, a)[0]


def test_half_day_mismatch_is_reported():
    issues = cc.compare({date(2026, 12, 24): N}, {date(2026, 12, 24): HALF})
    assert issues == ["2026-12-24: Alpaca 09:30-16:00 vs NYSE calendar 09:30-13:00"]


def test_special_days_lists_only_non_normal_sessions():
    cal = {date(2026, 11, 25): N, date(2026, 11, 27): HALF}
    assert cc.special_days(cal) == {date(2026, 11, 27): HALF}


def test_alpaca_model_times_parse():
    from alpaca.trading.models import Calendar
    c = Calendar(date="2026-11-27", open="09:30", close="13:00")
    assert (cc._as_time(c.open), cc._as_time(c.close)) == HALF


def test_run_exit_codes(monkeypatch, capsys):
    cal = {date(2026, 11, 27): HALF}
    monkeypatch.setattr(cc, "alpaca_calendar", lambda s, e: cal)
    monkeypatch.setattr(cc, "nyse_calendar", lambda s, e: cal)
    assert cc.run(30, today=date(2026, 11, 20)) == 0
    monkeypatch.setattr(cc, "nyse_calendar", lambda s, e: {date(2026, 11, 27): N})
    assert cc.run(30, today=date(2026, 11, 20)) == 1

    def boom(s, e):
        raise ConnectionError("down")
    monkeypatch.setattr(cc, "alpaca_calendar", boom)
    assert cc.run(30, today=date(2026, 11, 20)) == 2  # fail closed, never "agree"
    assert "UNVERIFIED" in capsys.readouterr().out


def test_nyse_calendar_knows_2026_holidays_and_half_days():
    pytest.importorskip("pandas_market_calendars")
    cal = cc.nyse_calendar(date(2026, 11, 23), date(2026, 12, 31))
    assert date(2026, 11, 26) not in cal          # Thanksgiving
    assert cal[date(2026, 11, 27)] == HALF        # day after Thanksgiving
    assert cal[date(2026, 12, 24)] == HALF        # Christmas Eve
    assert date(2026, 12, 25) not in cal          # Christmas
    assert cal[date(2026, 12, 28)] == N
