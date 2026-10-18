"""Tests for deadman.py (own dead-man monitor). Fake clock, fake Telegram, real HTTP handler."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import deadman as dm

SECRET = "s3cret-token-0123456789"


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t
    def __call__(self):
        return self.t


def mk(tmp_path=None, ok=True, **kw):
    sent, clock = [], Clock()
    def send(text):
        if not ok_flag["ok"]:
            return False
        sent.append(text)
        return True
    ok_flag = {"ok": ok}
    m = dm.Monitor({"aether": SECRET}, grace_s=240, remind_s=3600, daily_hour=-1, send=send, clock=clock,
                   state_path=(tmp_path / "s.json") if tmp_path else None, **kw)
    return m, sent, clock, ok_flag


def test_no_alert_while_pings_arrive():
    m, sent, clock, _ = mk()
    for _ in range(10):
        assert m.ping(SECRET) == "aether"
        clock.t += 60
        m.tick()
    assert sent == []


def test_missing_pings_alert_once_then_recovery():
    m, sent, clock, _ = mk()
    m.ping(SECRET)
    clock.t += 200; m.tick(); assert sent == []                 # inside the grace period
    clock.t += 100; m.tick()
    assert len(sent) == 1 and "no check-in" in sent[0] and "aether" in sent[0]
    clock.t += 60; m.tick(); assert len(sent) == 1               # not repeated every tick
    clock.t += 600
    m.ping(SECRET)
    m.tick()
    assert any("back after" in s for s in sent) and m.status(SECRET)["up"] is True


def test_reminder_while_still_down():
    m, sent, clock, _ = mk()
    m.ping(SECRET); clock.t += 300; m.tick()
    n = len(sent)
    clock.t += 3700; m.tick()
    assert len(sent) == n + 1 and "still down" in sent[-1]


def test_fail_endpoint_alerts_immediately():
    m, sent, clock, _ = mk()
    m.ping(SECRET)
    m.ping(SECRET, "fail"); m.tick()
    assert len(sent) == 1 and "reported a failure" in sent[0] and m.status(SECRET)["up"] is False


def test_wrong_secret_is_rejected_and_never_alerts():
    m, sent, clock, _ = mk()
    assert m.ping("wrong") is None and m.status("wrong") is None
    m.tick()
    assert sent == []


def test_never_checked_in_alerts_after_three_graces():
    m, sent, clock, _ = mk()
    clock.t += 700; m.tick()
    assert sent == []
    clock.t += 100; m.tick()
    assert len(sent) == 1 and "ever arrived" in sent[0]


def test_undelivered_alert_is_retried_not_lost(tmp_path):
    m, sent, clock, flag = mk(tmp_path, ok=False)
    m.ping(SECRET); clock.t += 300; m.tick()
    assert sent == [] and m.status(SECRET)["queued_alerts"] == 1
    flag["ok"] = True
    clock.t += 15; m.tick()
    assert len(sent) == 1 and m.status(SECRET)["queued_alerts"] == 0


def test_state_survives_restart(tmp_path):
    m, sent, clock, _ = mk(tmp_path)
    m.ping(SECRET); clock.t += 300; m.tick()
    sent2 = []
    m2 = dm.Monitor({"aether": SECRET}, grace_s=240, remind_s=3600, daily_hour=-1, send=lambda t: sent2.append(t) or True, clock=clock, state_path=tmp_path / "s.json")
    clock.t += 30; m2.tick()
    assert sent2 == [] and m2.status(SECRET)["status"] == "down"   # remembers it is down, does not alert again
    m2.ping(SECRET); m2.tick()
    assert any("back after" in s for s in sent2)


def test_daily_message_sent_once_per_day():
    m, sent, clock, _ = mk()
    m.daily_hour = 1_800_000_000 // 3600 % 24
    m.ping(SECRET)
    m.tick(); m.tick()
    assert sum("Monitor alive" in s for s in sent) == 1
    clock.t += 86400; m.ping(SECRET); m.tick()
    assert sum("Monitor alive" in s for s in sent) == 2


def test_http_endpoints():
    m, sent, clock, _ = mk()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dm.make_handler(m))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        assert urllib.request.urlopen(f"{base}/ping/{SECRET}").read() == b"OK"
        assert urllib.request.urlopen(urllib.request.Request(f"{base}/ping/{SECRET}", method="POST")).status == 200
        st = json.loads(urllib.request.urlopen(f"{base}/status/{SECRET}").read())
        assert st["up"] is True and st["last_ping_age_s"] == 0
        assert urllib.request.urlopen(f"{base}/healthz").read() == b"OK"
        for bad in ("/ping/nope", "/status/nope", "/ping", "/"):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + bad)
            assert e.value.code == 404
        urllib.request.urlopen(f"{base}/ping/{SECRET}/fail")
        assert json.loads(urllib.request.urlopen(f"{base}/status/{SECRET}").read())["up"] is False
    finally:
        srv.shutdown()


def test_main_refuses_weak_or_missing_secret(monkeypatch):
    monkeypatch.delenv("DEADMAN_SECRET", raising=False); monkeypatch.delenv("DEADMAN_CHECKS", raising=False)
    assert dm.main() == 2
    monkeypatch.setenv("DEADMAN_SECRET", "short")
    assert dm.main() == 2


def test_probe_in_connectivity_reads_monitor_status(monkeypatch):
    import connectivity as cx
    monkeypatch.delenv("HEALTHCHECK_URL", raising=False); monkeypatch.delenv("HEALTHCHECK_PING_URL", raising=False)
    assert cx.p_monitor()[0] == "off"
    monkeypatch.setenv("HEALTHCHECK_URL", "https://hc-ping.com/abc")
    assert cx.p_monitor()[0] == "unknown"
    m, sent, clock, _ = mk()
    m.clock = None
    live = dm.Monitor({"aether": SECRET}, grace_s=240, daily_hour=-1)
    live.ping(SECRET)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dm.make_handler(live))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("HEALTHCHECK_URL", f"http://127.0.0.1:{srv.server_address[1]}/ping/{SECRET}")
        assert cx.p_monitor()[0] == "up"
        live.ping(SECRET, "fail")
        assert cx.p_monitor()[0] == "down"
    finally:
        srv.shutdown()


def test_pings_and_probe_send_a_real_user_agent(monkeypatch):
    """Cloudflare answers 403 to Python's default agent while curl gets 200: every caller must set one."""
    import urllib.request, aether_ops, connectivity as cx
    seen = []
    class R:
        def read(self): return b'{"up": true, "last_ping_age_s": 1, "grace_s": 240}'
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake(req, timeout=None):
        seen.append(req); return R()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setenv("HEALTHCHECK_URL", "https://m.example/ping/" + SECRET)
    aether_ops.ping("https://m.example/ping/" + SECRET)
    assert cx.p_monitor()[0] == "up"
    assert len(seen) == 2
    for req in seen:
        assert not isinstance(req, str) and req.get_header("User-agent", "").startswith("AETHER")
