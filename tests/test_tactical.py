"""Tactical paper tests: both engines, the simulator, the ledger verdict, and the read-only API."""
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from aether_tactical import engulfing_engine as eng, ledger, orb_engine as orb, run_day
from aether_tactical.bars import Bar, BarError, regular_hours, verify
from aether_tactical.market_time import Session
from aether_tactical.sim import simulate

NY = ZoneInfo("America/New_York")
S = Session("2026-10-20", datetime(2026, 10, 20, 9, 30, tzinfo=NY), datetime(2026, 10, 20, 16, 0, tzinfo=NY))


def day(prices, start=S.open, vols=None):
    """1-minute bars from a list of (o, h, l, c) or closes."""
    out = []
    for i, p in enumerate(prices):
        o, h, l, c = p if isinstance(p, tuple) else (p, p + 0.05, p - 0.05, p)
        out.append(Bar(start + timedelta(minutes=i), o, h, l, c, (vols[i] if vols else 1000.0)))
    return out


def flat_day(n=390, px=500.0):
    return day([px] * n)


# ---------- ORB --------------------------------------------------------------------------------
def test_orb_breakout_trade_and_stop():
    prices = [500.0] * 15 + [500.0] * 10 + [(500.0, 500.6, 499.9, 500.5)] + [501.0] * 364
    r = orb.evaluate(day(prices), S)
    assert r["state"] == "traded" and r["range_high"] == 500.05
    t = r["trade"]
    assert t["entry_time"] == "09:56" and t["entry_px"] == 501.0 and t["exit_reason"] == "flat_before_close"
    assert t["exit_time"] == "15:55" and t["net_bp"] == round((501.0 / 501.0 - 1) * 1e4 - 4, 2)


def test_orb_needs_all_15_range_bars_and_respects_cutoff():
    bars = flat_day()
    del bars[3]
    assert orb.evaluate(bars, S)["reason"].startswith("incomplete range")
    assert orb.evaluate(flat_day(), S)["state"] == "no_trade"
    assert orb.evaluate(flat_day(10), S)["state"] == "forming_range"
    late = [500.0] * 125 + [(500.0, 501, 499.9, 501.0)] + [501.0] * 264   # breakout at 11:35 > cutoff
    assert orb.evaluate(day(late), S)["state"] == "no_trade"


def test_orb_cutoff_on_early_close():
    early = Session("2026-11-27", datetime(2026, 11, 27, 9, 30, tzinfo=NY), datetime(2026, 11, 27, 13, 0, tzinfo=NY))
    assert orb.entry_cutoff(early) == datetime(2026, 11, 27, 11, 30, tzinfo=NY)
    very_early = Session("x", early.open, datetime(2026, 11, 27, 12, 0, tzinfo=NY))
    assert orb.entry_cutoff(very_early) == datetime(2026, 11, 27, 11, 0, tzinfo=NY)


# ---------- simulator -------------------------------------------------------------------------
def test_stop_fills_at_worse_of_open_and_stop_and_stop_beats_target_in_one_bar():
    bars = day([(500, 500.1, 499.9, 500), (499.0, 499.2, 498.5, 499.0)])
    assert simulate(bars, 0, 499.5, None, S)["exit_px"] == 499.0          # gap through the stop
    both = day([(500, 500.1, 499.9, 500), (500, 502, 499, 501)])
    assert simulate(both, 0, 499.5, 501.5, S)["exit_reason"] == "stop"


# ---------- engulfing -------------------------------------------------------------------------
def engulf_day(vol_ok=True, mult=None, base=1000.0):
    # 15-min bars: 0..4 flat (09:30-10:45), bar 5 (10:45-10:59) red, bar 6 (11:00-11:14) green engulfing
    # -> ends 11:15. The engulfing bar's volume is 2x the median of the five bars before it (ok) or 1.4x.
    p = [500.0] * 75
    red = [(501.0, 501.05, 500.0, 500.9)] + [500.5] * 13 + [(500.3, 500.35, 500.1, 500.2)]
    green = [(500.1, 500.2, 499.8, 500.3)] + [500.8] * 13 + [(501.0, 501.3, 500.9, 501.2)]
    rest = [501.2] * (390 - 105)
    vols = [base] * 90 + [(mult if mult is not None else (2.0 if vol_ok else 1.4)) * base] * 15 + [base] * len(rest)
    return day(p + red + green + rest, vols=vols)


def test_engulfing_signal_entry_and_target():
    r = eng.evaluate(engulf_day(), S)
    assert r["state"] == "traded", r
    t = r["trade"]
    assert t["entry_time"] == "11:15" and t["stop"] == 499.8
    assert t["target"] == round(t["entry_px"] + 1.5 * (t["entry_px"] - 499.8), 4)


def test_engulfing_needs_1_5x_median_of_previous_five_bars():
    assert eng.evaluate(engulf_day(vol_ok=False), S)["state"] == "no_trade"      # 1.4x: too weak
    assert eng.evaluate(engulf_day(mult=1.5), S)["state"] == "traded"           # exactly 1.5x passes
    assert eng.evaluate(engulf_day(mult=1.49), S)["state"] == "no_trade"


def test_engulfing_ignores_signals_before_five_prior_bars():
    # same pattern one bar earlier (bar k = 4, only four bars before it): never a signal
    p = [500.0] * 45
    red = [(501.0, 501.05, 500.0, 500.9)] + [500.5] * 13 + [(500.3, 500.35, 500.1, 500.2)]
    green = [(500.1, 500.2, 499.8, 500.3)] + [500.8] * 13 + [(501.0, 501.3, 500.9, 501.2)]
    rest = [501.2] * (390 - 75)
    vols = [1000.0] * 60 + [5000.0] * 15 + [1000.0] * len(rest)
    r = eng.evaluate(day(p + red + green + rest, vols=vols), S)
    assert r["state"] == "no_trade" and r["signal"] == "NO_SIGNAL"


# ---------- bars ------------------------------------------------------------------------------
def test_regular_hours_drops_premarket_and_verify_rejects_bad_bars():
    pre = Bar(S.open - timedelta(minutes=5), 500, 500, 500, 500, 1)
    assert regular_hours([pre] + flat_day(3), S)[0].t == S.open
    with pytest.raises(BarError):
        verify(day([500.0, 540.0]), S)
    with pytest.raises(BarError):
        verify([Bar(S.open, 500, 499, 501, 500, 1)], S)


# ---------- ledger and runner -----------------------------------------------------------------
def test_ledger_is_append_only_and_verdict_waits_for_40_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("TACTICAL_LEDGER", str(tmp_path / "l.jsonl"))
    row = {"day": "2026-10-20", "strategy": "orb15", "state": "traded", "trade": {"net_bp": 5.0}}
    assert ledger.append(row) and not ledger.append(row)
    assert ledger.summary()["orb15"]["verdict"].startswith("collecting (1 of 40")
    for i in range(45):
        ledger.append({"day": f"d{i}", "strategy": "x", "state": "traded", "trade": {"net_bp": -1.0 + (i % 2) * 0.5}})
    assert ledger.summary()["x"]["verdict"] == "unsupported"


def test_run_day_scores_last_finished_session_once(tmp_path, monkeypatch):
    monkeypatch.setenv("TACTICAL_LEDGER", str(tmp_path / "l.jsonl"))
    monkeypatch.setattr(run_day, "now_ny", lambda: datetime(2026, 10, 21, 10, 0, tzinfo=NY))   # today unfinished
    lookup = lambda d: S if d == date(2026, 10, 20) else (Session("2026-10-21", S.open + timedelta(days=1), S.close + timedelta(days=1)) if d == date(2026, 10, 21) else None)
    res = run_day.score(date(2026, 10, 21), fetch=lambda sym, s, now: flat_day(), session_lookup=lookup)
    assert {r["day"] for r in res} == {"2026-10-20"} and len(ledger.rows()) == 2
    run_day.score(date(2026, 10, 21), fetch=lambda sym, s, now: flat_day(), session_lookup=lookup)
    assert len(ledger.rows()) == 2


def test_data_error_recorded_as_no_data(tmp_path, monkeypatch):
    monkeypatch.setenv("TACTICAL_LEDGER", str(tmp_path / "l.jsonl"))
    monkeypatch.setattr(run_day, "now_ny", lambda: datetime(2026, 10, 20, 17, 0, tzinfo=NY))
    def boom(*a):
        raise BarError("down")
    res = run_day.score(date(2026, 10, 20), fetch=boom, session_lookup=lambda d: S if d == date(2026, 10, 20) else None)
    assert all(r["state"] == "no_data" for r in res)


# ---------- read-only, mounted, deployed, written down ----------------------------------------
def test_read_only_mounted_and_preregistered():
    root = Path(__file__).resolve().parent.parent
    src = "".join(p.read_text() for p in (root / "aether_tactical").glob("*.py"))
    for bad in ("submit_order", "cancel_order", "APCA_LIVE", "_canary_submit"):
        assert bad not in src
    import aether_engine as e
    assert {"/tactical/signal/{symbol}", "/tactical/ledger"} <= {getattr(r, "path", None) for r in e.app.routes}
    assert "Mon..Fri *-*-* 16:20:00 America/New_York" in (root / "deploy" / "aether-tactical.timer").read_text()
    assert "Tactical paper tests (pre-registered 2026-10-21" in (root / "strategy.md").read_text()


def test_signal_names():
    assert orb.evaluate(flat_day(10), S)["signal"] == "WAITING_FOR_RANGE"
    up = [500.0] * 25 + [(500.0, 500.6, 499.9, 500.5)] + [501.0] * 364
    assert orb.evaluate(day(up), S)["signal"] == "LONG_BREAKOUT"
    down = [500.0] * 25 + [(500.0, 500.1, 499.0, 499.5)] + [499.5] * 364
    r = orb.evaluate(day(down), S)
    assert r["signal"] == "SHORT_BREAKOUT" and r["trade"] is None     # information only, long-only test
    assert orb.evaluate(flat_day(), S)["signal"] == "NO_SIGNAL"
    assert eng.evaluate(engulf_day(), S)["signal"] == "BULLISH_ENGULFING"
    assert eng.evaluate(flat_day(60), S)["signal"] == "SCANNING"


def test_tactical_page_served_and_linked_everywhere():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from aether_tactical import api_tactical
    app = FastAPI(); app.include_router(api_tactical.build_router())
    page = TestClient(app).get("/tactical")
    assert page.status_code == 200 and "Paper tests only" in page.text
    root = Path(__file__).resolve().parent.parent
    for f in ("board.html", "trend.html", "tactical.html"):
        txt = (root / f).read_text()
        assert all(h in txt for h in ('href="/"', 'href="/trend"', 'href="/tactical"')), f
    import re
    t = (root / "tactical.html").read_text()
    assert not re.search(r"\$\d", t) and "math.random" not in t.lower() and 'method: "POST"' not in t
