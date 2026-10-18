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
def fresh(monkeypatch, tmp_path):
    import notify
    monkeypatch.setenv("CONNECTIVITY_BG", "0")
    monkeypatch.setenv("CONNECTIVITY_ALERTS", "0")
    monkeypatch.setattr(cx, "EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(notify, "LOG_FILE", tmp_path / "tg.jsonl")
    notify.RESP_LOG.clear()
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


def _flaky(seq):
    it = iter(seq)
    return lambda: next(it)


def test_alert_after_two_failures_then_recovery_message(monkeypatch):
    sent = []
    monkeypatch.setenv("CONNECTIVITY_ALERTS", "1")
    import notify
    monkeypatch.setattr(notify, "send", lambda text, key=None, cooldown=0: sent.append((key, text)) or True)
    f = _flaky([("down", "refused"), ("down", "refused"), ("down", "refused"), ("up", "ok")])
    cx.run_once({"github": f})
    assert sent == []                                  # one failed check is not an outage
    cx.run_once({"github": f})
    assert len(sent) == 1 and sent[0][0] == "conn-down-github" and "GitHub is disconnected" in sent[0][1] and "Fix:" in sent[0][1]
    cx.run_once({"github": f})
    assert len(sent) == 1                              # no repeat while it stays down
    cx._state["github"]["alert_since"] -= 300
    cx.run_once({"github": f})
    assert sent[-1][0] == "conn-up-github" and "back after" in sent[-1][1]
    evs = cx.recent_events()
    assert [e["ev"] for e in evs] == ["up", "down"] and evs[1]["id"] == "github"


def test_telegram_outage_is_reported_on_recovery_not_by_telegram(monkeypatch):
    sent = []
    monkeypatch.setenv("CONNECTIVITY_ALERTS", "1")
    import notify
    monkeypatch.setattr(notify, "send", lambda text, key=None, cooldown=0: sent.append((key, text)) or True)
    f = _flaky([("down", "x"), ("down", "x"), ("up", "ok")])
    for _ in range(3):
        cx.run_once({"telegram": f})
    assert [k for k, _ in sent] == ["conn-up-telegram"] and "held until Telegram came back" in sent[0][1]


def test_flapping_single_failure_never_alerts(monkeypatch):
    sent = []
    monkeypatch.setenv("CONNECTIVITY_ALERTS", "1")
    import notify
    monkeypatch.setattr(notify, "send", lambda *a, **k: sent.append(a) or True)
    f = _flaky([("down", "x"), ("up", "ok"), ("down", "x"), ("up", "ok")])
    for _ in range(4):
        cx.run_once({"alpaca_paper": f})
    assert sent == [] and cx.recent_events() == []


def test_down_since_survives_a_restart(monkeypatch):
    f = _flaky([("down", "x"), ("down", "x")])
    cx.run_once({"tunnel": f}); cx.run_once({"tunnel": f})
    since = cx._state["tunnel"]["state_since"]
    cx._state.clear()                                   # simulate the engine restarting
    cx.run_once({"tunnel": lambda: ("down", "still down")})
    assert abs(cx._state["tunnel"]["state_since"] - since) < 2
    assert cx._state["tunnel"]["alerted"] is True       # no second alert after the restart


def test_alerts_can_be_switched_off(monkeypatch):
    sent = []
    import notify
    monkeypatch.setattr(notify, "send", lambda *a, **k: sent.append(a) or True)
    for _ in range(3):
        cx.run_once({"github": lambda: ("down", "x")})
    assert sent == [] and cx.recent_events() != []


def test_telegram_replies_are_recorded_without_secrets(monkeypatch):
    import io, notify
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRETTOKEN"); monkeypatch.setenv("TELEGRAM_CHAT_ID", "777")
    class R:
        status = 200
        def __init__(self, b): self.b = b
        def read(self): return self.b
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(notify.urllib.request, "urlopen", lambda req, timeout=0: R(json.dumps({"ok": True, "result": {"message_id": 42}}).encode()))
    notify._call("sendMessage", {"chat_id": "777", "text": "secret text"})
    def err(req, timeout=0):
        raise urllib.error.HTTPError("u", 429, "Too Many", {}, io.BytesIO(b'{"description":"retry 123:SECRETTOKEN","parameters":{"retry_after":5}}'))
    monkeypatch.setattr(notify.urllib.request, "urlopen", err)
    with pytest.raises(urllib.error.HTTPError) as ei:
        notify._call("sendMessage", {"chat_id": "777", "text": "x"})
    assert json.loads(ei.value.read())["parameters"]["retry_after"] == 5     # retry logic still sees the body
    rows = notify.recent_responses()
    assert rows[1]["ok"] and rows[1]["mid"] == 42 and rows[0]["http"] == 429 and not rows[0]["ok"]
    blob = json.dumps(rows) + notify.LOG_FILE.read_text()
    assert "SECRETTOKEN" not in blob and "secret text" not in blob and "777" not in blob


def test_empty_long_poll_is_not_logged(monkeypatch):
    import notify
    class R:
        status = 200
        def read(self): return b'{"ok":true,"result":[]}'
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    monkeypatch.setattr(notify.urllib.request, "urlopen", lambda req, timeout=0: R())
    notify._call("getUpdates", {"offset": 0})
    assert notify.recent_responses() == []


def test_route_includes_events_and_telegram_log(monkeypatch):
    import notify
    monkeypatch.setattr(cx, "PROBES", {"internet": lambda: ("up", "ok")})
    monkeypatch.setattr(notify, "recent_responses", lambda n=30: [{"m": "sendMessage", "ok": True, "ms": 120, "http": 200, "mid": 5, "ts": 1.0}])
    app = FastAPI(); app.include_router(cx.build_router(lambda: None))
    j = TestClient(app).get("/api/connectivity").json()
    assert j["telegram_log"]["sent_ok"] == 1 and j["telegram_log"]["rows"][0]["mid"] == 5 and j["events"] == []
