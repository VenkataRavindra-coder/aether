"""/api/us/extras: read-only, fails soft per section, never leaks secrets."""
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import us_extras  # noqa: E402

ORDERS = [
    {"symbol": "SPY", "side": "buy", "type": "limit", "status": "filled", "qty": "2",
     "submitted_at": "2026-10-13T13:45:00Z", "filled_at": "2026-10-13T13:45:02.5Z", "asset_class": "us_equity"},
    {"symbol": "TLT", "side": "sell", "type": "limit", "status": "rejected", "qty": "1",
     "submitted_at": "2026-10-13T13:45:00Z", "filled_at": None, "asset_class": "us_equity"},
    {"symbol": "BTC/USD", "side": "buy", "type": "market", "status": "filled", "asset_class": "crypto",
     "submitted_at": "2026-10-13T13:45:00Z", "filled_at": "2026-10-13T13:45:01Z"},
]


def _fake_alpaca(acct, path):
    if path.startswith("/v2/orders"):
        return ORDERS
    if path.startswith("/v2/positions"):
        return [{"symbol": "SPY", "asset_class": "us_equity"}]
    raise AssertionError(path)


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    monkeypatch.setattr(us_extras, "_cache", {"t": 0.0, "v": None})
    monkeypatch.setattr(us_extras, "REPO", tmp_path)

    def make(alpaca=_fake_alpaca):
        monkeypatch.setattr(us_extras.usd, "alpaca_get", alpaca)
        app = FastAPI()
        app.include_router(us_extras.build_router(lambda: None))
        return TestClient(app)
    return make


def test_orders_lifecycle_latency_and_us_only(make_client):
    body = make_client().get("/api/us/extras").json()
    o = body["orders"]
    assert o["ok"] and o["account"] == "paper"
    syms = [r["symbol"] for r in o["orders"]]
    assert syms == ["SPY", "TLT"]                      # crypto filtered out
    spy, tlt = o["orders"]
    assert spy["fill_latency_sec"] == 2.5 and spy["holding"] is True
    assert tlt["fill_latency_sec"] is None and tlt["holding"] is False


def test_live_flag_switches_account(make_client, monkeypatch):
    seen = []
    monkeypatch.setenv("US_LIVE", "true")
    make_client(lambda acct, path: seen.append(acct) or _fake_alpaca(acct, path)).get("/api/us/extras")
    assert seen and set(seen) == {"live"}


def test_broker_down_fails_soft_others_still_work(make_client):
    def boom(*a, **k):
        raise RuntimeError("broker unreachable")
    body = make_client(boom).get("/api/us/extras").json()
    assert body["orders"]["ok"] is False and "broker unreachable" in body["orders"]["error"]
    assert body["server"]["ok"] is True


def test_missing_research_db_is_reported_not_invented(make_client):
    r = make_client().get("/api/us/extras").json()["research"]
    assert r["ok"] is False and "dsr" not in r


def test_research_counts_trials_and_reads_dsr(make_client, tmp_path):
    rdir = tmp_path / "aether-research"
    (rdir / "us_momentum" / "results").mkdir(parents=True)
    con = sqlite3.connect(rdir / "research.sqlite")
    con.execute("CREATE TABLE research_trials (id INTEGER PRIMARY KEY)")
    con.execute("CREATE TABLE untracked_trials (id INTEGER PRIMARY KEY, count INTEGER)")
    con.execute("INSERT INTO research_trials DEFAULT VALUES")
    con.execute("INSERT INTO untracked_trials (count) VALUES (28)")
    con.commit(); con.close()
    (rdir / "us_momentum" / "results" / "strategy_truth_yf_1.json").write_text('{"items":[{"dsr":0.8018}]}')
    r = make_client().get("/api/us/extras").json()["research"]
    assert r["ok"] and r["trials_total"] == 29 and abs(r["dsr"] - 0.8018) < 1e-9


def test_backups_none_then_found(make_client, tmp_path):
    assert make_client().get("/api/us/extras").json()["backups"]["last"] is None
    us_extras._cache.update(t=0.0, v=None)
    (tmp_path / "backups").mkdir()
    (tmp_path / "backups" / "aether-20261013T075659Z.tar.gz").write_bytes(b"x" * 2048)
    b = make_client().get("/api/us/extras").json()["backups"]
    assert b["ok"] and b["count"] == 1 and b["size_kb"] == 2


def test_no_secrets_in_response(make_client, monkeypatch):
    monkeypatch.setenv("APCA_API_SECRET_KEY", "SEKRET-should-never-appear")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:TOKEN-should-never-appear")
    monkeypatch.setenv("BACKUP_SAS_URL", "https://x/?sig=SAS-should-never-appear")
    text = make_client().get("/api/us/extras").text
    assert "never-appear" not in text


def test_router_is_read_only():
    r = us_extras.build_router(lambda: None)
    assert {m for route in r.routes for m in route.methods} == {"GET"}
