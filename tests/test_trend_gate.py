"""Trend Gate: the rule, data verification, safe failure and the endpoint. Offline (fake fetcher)."""
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from trend_gate import api
from trend_gate.config import Settings
from trend_gate.data import Bar, DataError, completed_bars, verify_bars
from trend_gate.service import SignalService
from trend_gate.strategy import RISK_OFF, RISK_ON, evaluate

NOW = datetime(2026, 10, 21, 22, 0, tzinfo=timezone.utc)   # 18:00 New York, after the close


def bars(closes, last="2026-10-21"):
    d = date.fromisoformat(last)
    days = [(d - timedelta(days=i)).isoformat() for i in range(len(closes))][::-1]
    return [Bar(x, float(c)) for x, c in zip(days, closes)]


def test_rule_above_and_below_the_average():
    up = evaluate("SPY", ["a", "b", "c"], [10.0, 10.0, 13.0], window=3)
    assert up.status == RISK_ON and up.action == "BUY / HOLD_LONG" and up.sma == 11.0
    down = evaluate("SPY", ["a", "b", "c"], [12.0, 12.0, 9.0], window=3)
    assert down.status == RISK_OFF and down.action == "DEFENSIVE_CASH / EXIT"
    assert evaluate("SPY", ["a", "b"], [10.0, 10.0], window=2).status == RISK_OFF   # equal is not "above"


def test_band_keeps_previous_status_near_the_average():
    s = evaluate("SPY", ["a", "b"], [100.0, 100.5], window=2, band_pct=1.0, previous_status=RISK_OFF)
    assert s.status == RISK_OFF and s.held_by_band


def test_verification_fails_closed():
    st = Settings(sma_window=5)
    verify_bars("SPY", bars([100] * 5), st, NOW)
    with pytest.raises(DataError, match="only 4"):
        verify_bars("SPY", bars([100] * 4), st, NOW)
    with pytest.raises(DataError, match="stale"):
        verify_bars("SPY", bars([100] * 5, last="2026-10-10"), st, NOW)
    with pytest.raises(DataError, match="bad data"):
        verify_bars("SPY", bars([100, 100, 100, 100, 160]), st, NOW)
    dup = bars([100] * 5)
    dup[-1] = Bar(dup[-2].date, 100.0)
    with pytest.raises(DataError, match="strictly increasing"):
        verify_bars("SPY", dup, st, NOW)


def test_partial_bar_dropped_during_session():
    during = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)   # 11:00 New York
    assert len(completed_bars(bars([1, 2, 3]), during)) == 2
    assert len(completed_bars(bars([1, 2, 3]), NOW)) == 3


class Fetcher:
    def __init__(self, result):
        self.result = result

    def fetch(self, symbol, now=None):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_service_serves_last_good_as_stale_then_no_signal(monkeypatch):
    monkeypatch.setattr("trend_gate.service.datetime", type("D", (), {"now": staticmethod(lambda tz=None: NOW)}))
    f = Fetcher(bars([100] * 4 + [110]))
    svc = SignalService(Settings(sma_window=5, cache_ttl_s=0, max_stale_s=3600), f)
    assert svc.get("spy")["status"] == RISK_ON
    f.result = TimeoutError("broker timeout")
    out = svc.get("SPY")
    assert out["ok"] and out["stale"] and "timeout" in out["error"]
    svc.settings = Settings(sma_window=5, cache_ttl_s=0, max_stale_s=0)
    out = svc.get("SPY")
    assert not out["ok"] and out["status"] == "NO_SIGNAL"
    with pytest.raises(KeyError):
        svc.get("TSLA")


def test_endpoint_codes(monkeypatch):
    monkeypatch.setattr("trend_gate.service.datetime", type("D", (), {"now": staticmethod(lambda tz=None: NOW)}))
    monkeypatch.setattr(api, "_service", SignalService(Settings(sma_window=5, cache_ttl_s=0), Fetcher(bars([100] * 5))))
    c = TestClient(api.create_app())
    r = c.get("/signal/SPY")
    assert r.status_code == 200 and r.json()["action"] == "DEFENSIVE_CASH / EXIT" and r.json()["sma"] == 100.0
    assert c.get("/signal/QQQ").status_code == 404
    monkeypatch.setattr(api, "_service", SignalService(Settings(sma_window=5), Fetcher(DataError("no data"))))
    assert c.get("/signal/SPY").status_code == 503


def test_mounted_on_the_engine_and_read_only():
    import aether_engine as eng
    assert "/signal/{symbol}" in {getattr(r, "path", None) for r in eng.app.routes}
    from pathlib import Path
    src = "".join(p.read_text() for p in Path(api.__file__).parent.glob("*.py"))
    for bad in ("submit_order", "TradingClient", "cancel_order", "APCA_LIVE_API_KEY_ID\")"):
        assert bad not in src


def test_history_series_and_page(monkeypatch):
    monkeypatch.setattr("trend_gate.service.datetime", type("D", (), {"now": staticmethod(lambda tz=None: NOW)}))
    monkeypatch.setattr(api, "_service", SignalService(Settings(sma_window=3, cache_ttl_s=0), Fetcher(bars([100, 101, 102, 103, 104]))))
    c = TestClient(api.create_app())
    h = c.get("/signal/SPY/history?days=10").json()
    assert h["ok"] and h["points"] == [[p[0], p[1], p[2]] for p in h["points"]]
    assert [p[2] for p in h["points"]] == [101.0, 102.0, 103.0]            # rolling 3-day averages
    page = c.get("/trend")
    assert page.status_code == 200 and "Trend gate" in page.text and 'fetch(url' in page.text
    monkeypatch.setattr(api, "_service", SignalService(Settings(sma_window=3), Fetcher(DataError("down"))))
    assert c.get("/signal/SPY/history").status_code == 503


def test_pages_link_each_other_and_show_no_made_up_numbers():
    from pathlib import Path
    root = Path(api.__file__).resolve().parent.parent
    trend, board = (root / "trend.html").read_text(), (root / "board.html").read_text()
    assert 'href="/trend"' in board and 'href="/"' in trend
    import re
    assert not re.search(r"\$\d", trend) and "math.random" not in trend.lower()
    import aether_engine as eng
    assert {"/trend", "/signal/{symbol}/history"} <= {getattr(r, "path", None) for r in eng.app.routes}
