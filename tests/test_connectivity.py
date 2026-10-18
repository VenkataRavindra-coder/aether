"""Tests for connectivity.py. Offline: every network call is faked."""
import json
import subprocess
import urllib.error
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import connectivity as cx


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setenv("CONNECTIVITY_BG", "0")
    cx._state.clear()
    yield
    cx._state.clear()


def test_down_probe_reports_reason_and_since(monkeypatch):
    def boom():
        raise OSError("connection refused")
    cx.run_once({"telegram": boom, "github": lambda: ("up", "ok")})
    s = cx.snapshot()
    assert s["probes"]["telegram"]["state"] == "down" and "connection refused" in s["probes"]["telegram"]["detail"]
    assert s["probes"]["github"]["state"] == "up"
    assert s["down"] == ["telegram"] and s["probes"]["telegram"]["down_for_s"] is not None


def test_state_change_resets_since_and_keeps_last_up():
    calls = iter([("up", "a"), ("down", "b"), ("down", "b")])
    cx.run_once({"x": lambda: next(calls)})
    up_at = cx._state["x"]["last_up"]
    cx._state["x"]["state_since"] -= 100   # pretend the up state lasted a while
    cx.run_once({"x": lambda: next(calls)})
    r = cx._state["x"]
    assert r["state"] == "down" and r["last_up"] == up_at and r["state_since"] > up_at - 1
    cx.run_once({"x": lambda: next(calls)})
    assert list(cx._state["x"]["history"]) == ["up", "down", "down"]


def test_http_error_is_down_and_secrets_scrubbed(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRET")
    def f():
        raise RuntimeError("failed https://x/bot123:SECRET/getMe")
    r = cx._run_one("telegram", f)
    assert r["state"] == "down" and "SECRET" not in r["detail"]
    def g():
        raise urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
    assert "401" in cx._run_one("alpaca_live", g)["detail"]


def test_github_probe_current_and_behind(monkeypatch):
    out = {"ls": "aaaaaaa1111\trefs/heads/main\n", "head": "aaaaaaa1111\n"}
    def run(cmd, **k):
        return SimpleNamespace(returncode=0, stdout=out["ls"] if cmd[1] == "ls-remote" else out["head"], stderr="")
    monkeypatch.setattr(cx.subprocess, "run", run)
    assert "latest code" in cx.p_github()[1]
    out["head"] = "bbbbbbb2222\n"
    st, d = cx.p_github()
    assert st == "up" and "newer code" in d
    monkeypatch.setattr(cx.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=128, stdout="", stderr="fatal: Could not read from remote repository."))
    assert cx.p_github()[0] == "down"


def test_telegram_probe_uses_real_send_and_poll_evidence(monkeypatch):
    import notify, time as _t
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x"); monkeypatch.setenv("TELEGRAM_CHAT_ID", "9")
    monkeypatch.setattr(cx, "_tg_get", lambda m, p=None: {"ok": True})
    monkeypatch.setattr(notify, "_dropped", 0)
    now = _t.time()
    monkeypatch.setattr(notify, "LINK", {"send_ok": now - 120, "send_err": None, "send_err_text": "", "poll_started": True, "poll_ok": now - 10, "poll_err": None})
    st, d = cx.p_telegram()
    assert st == "up" and "delivered 2 min ago" in d and "poller answered" in d
    notify.LINK.update(send_err=now - 30, send_err_text="HTTP 500")
    assert cx.p_telegram()[0] == "degraded"
    notify.LINK.update(send_err=None, poll_ok=now - 600)
    assert cx.p_telegram()[0] == "degraded"
    monkeypatch.setattr(cx, "_tg_get", lambda m, p=None: {"ok": m == "getMe"})
    st, d = cx.p_telegram()
    assert st == "down" and "chat" in d


def test_notify_marks_link_evidence(monkeypatch):
    import notify
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x"); monkeypatch.setenv("TELEGRAM_CHAT_ID", "9")
    monkeypatch.setattr(notify, "LINK", {"send_ok": None, "send_err": None, "send_err_text": "", "poll_started": False, "poll_ok": None, "poll_err": None})
    monkeypatch.setattr(notify, "_call", lambda *a, **k: {"ok": True})
    assert notify.send_sync("hi") and notify.LINK["send_ok"]
    def boom(*a, **k):
        raise OSError("x")
    monkeypatch.setattr(notify, "_call", boom)
    assert not notify.send_sync("hi") and notify.LINK["send_err"] and notify.LINK["send_err_text"] == "OSError"


def test_telegram_off_when_not_configured(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert cx.p_telegram()[0] == "off"


def test_tunnel_states(monkeypatch):
    monkeypatch.setattr(cx.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="active\n", returncode=0))
    assert cx.p_tunnel()[0] == "up"
    monkeypatch.setattr(cx.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="failed\n", returncode=3))
    assert cx.p_tunnel()[0] == "down"
    def nf(*a, **k):
        raise FileNotFoundError
    monkeypatch.setattr(cx.subprocess, "run", nf)
    assert cx.p_tunnel()[0] == "unknown"


def test_route_returns_json_and_you_block(monkeypatch):
    monkeypatch.setattr(cx, "PROBES", {"internet": lambda: ("up", "ok")})
    app = FastAPI()
    app.include_router(cx.build_router(lambda: None))
    c = TestClient(app)
    j = c.get("/api/connectivity", headers={"cf-ray": "abc"}).json()
    assert j["probes"]["internet"]["state"] == "up" and j["you"]["via_cloudflare"] is True
    assert c.get("/api/connectivity").json()["you"]["via_cloudflare"] is False
    json.dumps(j, allow_nan=False)


def test_engine_default_deny_covers_path():
    import aether_engine
    paths = {getattr(r, "path", "") for r in aether_engine.app.routes}
    assert "/api/connectivity" in paths
