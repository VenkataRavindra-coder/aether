"""python -m pytest -q test_us_dashboard.py"""
import json
import sqlite3
import time
from datetime import date

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import safety_guard as g
import us_dashboard as ud

FAKE = {
    ("live", "/v2/account"): {"equity": "100.00", "last_equity": "101.00", "cash": "60.00", "status": "ACTIVE"},
    ("live", "/v2/positions"): [{"symbol": "SPY", "qty": "0.07", "avg_entry_price": "560", "current_price": "571.4",
                                 "unrealized_pl": "0.8", "unrealized_intraday_pl": "-0.2", "asset_class": "us_equity"}],
    ("paper", "/v2/account"): {"equity": "248430", "last_equity": "247000", "cash": "142200"},
    ("paper", "/v2/positions"): [
        {"symbol": "QQQ", "qty": "3", "avg_entry_price": "480", "current_price": "490",
         "unrealized_pl": "30", "unrealized_intraday_pl": "6", "asset_class": "us_equity"},
        {"symbol": "BTCUSD", "qty": "0.01", "avg_entry_price": "60000", "current_price": "61000",
         "unrealized_pl": "10", "unrealized_intraday_pl": "1", "asset_class": "crypto"}],
    ("paper", "/v2/clock"): {"is_open": True},
}


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AETHER_HOME", str(tmp_path))
    for k in ("DECISION_DB", "DECISION_SQL", "US_STOPS_FILE", "US_STOPS_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(ud, "alpaca_get", lambda a, p: FAKE[(a, p)])
    ud._cache.clear()
    return tmp_path


def client(logged_in=True):
    def auth():
        if not logged_in:
            raise HTTPException(401)
    app = FastAPI()
    app.include_router(ud.build_router(auth))
    return TestClient(app)


def test_requires_login():
    c = client(logged_in=False)
    assert c.get("/us").status_code == 401
    assert c.get("/api/us/summary").status_code == 401
    assert c.post("/api/us/pause", headers={"x-aether-csrf": ud.CSRF_TOKEN}).status_code == 401


def test_us_page_redirects_to_console():
    # 2026-10-18: /us was merged into the single console at / (sections #us and #safety).
    r = client().get("/us", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/console#us"


def test_summary_us_only_and_limits_from_guard():
    d = client().get("/api/us/summary").json()
    assert [p["symbol"] for p in d["paper"]["positions"]] == ["QQQ"]      # crypto excluded
    assert d["risk"]["limits"] == g.limits().__dict__                       # single source
    assert d["risk"]["initialised"] is False and d["state"] == "engine_down"  # no heartbeat yet
    assert d["market_open"] is True


def test_risk_view_after_mark(env):
    g.mark(101, date(2020, 1, 6))
    d = client().get("/api/us/summary").json()["risk"]
    assert d["initialised"] and d["daily_loss"] == pytest.approx(1 / 101)


def test_pause_needs_csrf_and_only_creates_halt():
    c = client()
    assert c.post("/api/us/pause").status_code == 403
    assert c.post("/api/us/pause", headers={"x-aether-csrf": "wrong"}).status_code == 403
    assert not g.halt_file().exists()
    assert c.post("/api/us/pause", headers={"x-aether-csrf": ud.CSRF_TOKEN}).json() == {"halted": True}
    assert g.halt_file().exists()
    assert c.get("/api/us/summary").json()["state"] == "paused"


def test_no_resume_route():
    c = client()
    g.atomic_write(g.halt_file(), "x")
    for m, path in (("delete", "/api/us/pause"), ("post", "/api/us/resume"), ("get", "/api/us/resume")):
        assert getattr(c, m)(path).status_code in (404, 405)
    assert g.halt_file().exists()


def test_stops_states(env, monkeypatch):
    d = client().get("/api/us/summary").json()
    assert d["live"]["positions"][0]["stop_state"] == "not_connected"
    sf = env / "us_state.json"
    sf.write_text(json.dumps({"engine": {"stops": {"SPY": {"stop": 555.0}}}}))
    monkeypatch.setenv("US_STOPS_FILE", str(sf)); monkeypatch.setenv("US_STOPS_KEY", "engine.stops")
    ud._cache.clear()
    d = client().get("/api/us/summary").json()
    assert d["live"]["positions"][0]["stop"] == 555.0
    assert d["paper"]["positions"][0]["stop_state"] == "missing"            # QQQ has no stop -> flagged


def test_decision_log_read_only(env, monkeypatch):
    db = env / "decisions.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE decisions (ts TEXT, symbol TEXT, action TEXT, gate TEXT, reason TEXT)")
    con.execute("INSERT INTO decisions VALUES ('2026-10-05T15:55', 'SPY', 'BUY', 'allowed', 'trend up')")
    con.commit(); con.close()
    monkeypatch.setenv("DECISION_DB", str(db))
    s = client().get("/api/us/summary").json()["signals"]
    assert s["connected"] and s["rows"][0]["symbol"] == "SPY"
    monkeypatch.setenv("DECISION_SQL", "DELETE FROM decisions")             # write attempt must fail
    ud._cache.clear()
    s = client().get("/api/us/summary").json()["signals"]
    assert "error" in s
    assert sqlite3.connect(db).execute("SELECT count(*) FROM decisions").fetchone()[0] == 1


def test_broker_error_does_not_break_page(monkeypatch):
    def boom(a, p):
        raise RuntimeError("live API keys not configured")
    monkeypatch.setattr(ud, "alpaca_get", boom)
    d = client().get("/api/us/summary").json()
    assert "error" in d["live"] and "error" in d["paper"]


def test_running_state_with_fresh_heartbeat(env):
    (env / "heartbeat.json").write_text(json.dumps({"engine": {"ts": time.time()}}))
    assert client().get("/api/us/summary").json()["state"] == "running"
