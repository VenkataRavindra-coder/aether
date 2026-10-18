"""Tests for map_status.py (live system map). Offline: no systemd, no broker."""
import json
import os
import re
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import map_status as ms

SHOW = """Id=ratelark.service
LoadState=loaded
ActiveState=active
SubState=running
Result=success
ActiveEnterTimestamp=@1760760000

Id=cloudflared.service
LoadState=not-found
ActiveState=inactive
SubState=dead
Result=success

Id=aether-backup.timer
LoadState=loaded
ActiveState=active
SubState=waiting
LastTriggerUSec=@1760757300
NextElapseUSecRealtime=@1760843700

Id=aether-backup.service
LoadState=loaded
ActiveState=inactive
SubState=dead
Result=exit-code
"""


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    ms._cache.update(t=0.0, v=None)
    ms._acct_cache.update(t=0.0, v=None)
    monkeypatch.setenv("AETHER_SERVICE", "ratelark.service")
    monkeypatch.setenv("RISK_LOG", "0")
    yield


def test_parse_show_blocks():
    u = ms.parse_show(SHOW)
    assert set(u) == {"ratelark.service", "cloudflared.service", "aether-backup.timer", "aether-backup.service"}
    assert u["aether-backup.timer"]["LastTriggerUSec"] == "@1760757300"


def test_units_view_reports_jobs_and_failures():
    run = lambda *a, **k: SimpleNamespace(returncode=0, stdout=SHOW, stderr="")
    v = ms.units_view(run=run)
    assert v["engine"]["active"] == "active" and v["engine"]["since"].startswith("2025-10-18")
    assert v["tunnel"]["loaded"] is False
    b = v["jobs"]["backup"]
    assert b["timer_active"] and b["last_run"] and b["next_run"] and b["result"] == "exit-code"
    assert v["jobs"]["health"]["timer_loaded"] is False  # missing units are reported, not invented


def test_usec_handles_empty_and_zero():
    assert ms._usec("") is None and ms._usec("0") is None and ms._usec("n/a") is None
    assert ms._usec("@12") == 12


def test_gate_view_reads_halt_and_budgets(tmp_path, monkeypatch):
    import risk_controller as rc
    import safety_guard as sg
    monkeypatch.setenv("AETHER_HOME", str(tmp_path))
    monkeypatch.setenv("AETHER_RISK_STATE", str(tmp_path / "r.json"))
    monkeypatch.setenv("AETHER_CANARY_RISK_STATE", str(tmp_path / "c.json"))
    (tmp_path / "c.json").write_text(json.dumps({"day": rc._today(), "usd": 8}))
    sg.halt_file().touch()
    g = ms.gate_view()
    assert g["halt"] is True
    assert g["live_canary"]["spent"] == 8 and g["live_canary"]["cap_day"] == 30
    assert g["live_us"]["spent"] == 0
    assert g["ledger"]["initialised"] is False


def test_gate_view_unreadable_budget_is_none_not_inf(tmp_path, monkeypatch):
    monkeypatch.setenv("AETHER_HOME", str(tmp_path))
    monkeypatch.setenv("AETHER_RISK_STATE", str(tmp_path / "r.json"))
    (tmp_path / "r.json").write_text("{broken")
    g = ms.gate_view()
    assert g["live_us"]["spent"] is None
    json.dumps(g, allow_nan=False)  # must be valid JSON


def test_backups_daily_stress(tmp_path):
    (tmp_path / "backups").mkdir()
    (tmp_path / "backups" / "aether-20261018T031500Z.tar.gz").write_bytes(b"x" * 2048)
    d = tmp_path / "tests" / "evidence" / "daily"
    d.mkdir(parents=True)
    (d / "2026-10-18.json").write_text(json.dumps({"utc": "2026-10-18T04:31:00+00:00", "all_green": False,
                                                   "results": [{"id": "RD3", "ok": False, "detail": "no upload"}]}))
    (tmp_path / ".stress_last_run.json").write_text('{"rc": 0, "utc": "2026-10-17T16:04:00Z"}')
    b = ms.backups_view(tmp_path)
    assert b["count"] == 1 and b["age_h"] is not None
    assert ms.daily_view(tmp_path)["checks"] == [{"id": "RD3", "ok": False, "detail": "no upload"}]
    assert ms.stress_view(tmp_path) == {"rc": 0, "utc": "2026-10-17T16:04:00Z"}


def test_every_section_fails_soft_and_no_secrets(monkeypatch):
    monkeypatch.setenv("APCA_API_SECRET_KEY", "sekret-should-never-appear")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:token-should-never-appear")
    monkeypatch.setattr(ms, "units_view", lambda: (_ for _ in ()).throw(FileNotFoundError("systemctl")))
    monkeypatch.setattr(ms, "accounts_view", lambda: {"paper": {"ok": False, "error": "URLError"}})
    s = ms.build_status(lambda: {"us": {"note": "x"}})
    assert s["units"]["ok"] is False and "systemctl" in s["units"]["error"]
    assert s["engine"]["ok"] is True and s["engine"]["us"] == {"note": "x"}
    text = json.dumps(s, allow_nan=False)
    assert "should-never-appear" not in text


def test_routes_serve_page_and_status(monkeypatch):
    monkeypatch.setattr(ms, "accounts_view", lambda: {})
    app = FastAPI()
    app.include_router(ms.build_router(lambda: None))
    c = TestClient(app)
    r = c.get("/map")
    assert r.status_code == 200 and "AETHER Live Map" in r.text and r.headers["cache-control"] == "no-store"
    assert c.get("/api/map/status").json()["engine"]["ok"] is True


def test_page_never_injects_server_text_as_html():
    page = (ms.REPO / "map_view.html").read_text()
    assert "innerHTML" not in page and "insertAdjacentHTML" not in page
    assert re.search(r'fetch\("/api/map/status"', page)


def test_engine_registers_map_routes():
    import aether_engine as eng
    paths = {getattr(r, "path", None) for r in eng.app.routes}
    assert {"/map", "/api/map/status"} <= paths
    info = eng._map_engine_info()
    assert set(info) == {"us", "crypto"}
