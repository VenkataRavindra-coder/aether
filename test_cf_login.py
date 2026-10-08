"""Offline tests for password-free dashboard login via Cloudflare Access (the _dashboard_auth
middleware in aether_engine.py). cf_access's own JWT checks are covered by test_cf_access.py;
here verify()/configured() are stubbed so this tests only the middleware's decision.
Run: python3 test_cf_login.py
"""
import os
import importlib.util

os.environ.update(
    LIVE_TRADING="false", US_LIVE="false", US_EXECUTE="false", US_ENABLED="false",
    STRATEGY_ENABLED="false", TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID="",
    WEBHOOK_SECRET="test-webhook-secret", DASHBOARD_PASSWORD="",
    APCA_API_KEY_ID="", APCA_API_SECRET_KEY="", APCA_LIVE_API_KEY_ID="", APCA_LIVE_API_SECRET_KEY="",
    CF_ACCESS_TEAM_DOMAIN="", CF_ACCESS_AUD="",
)

PATH = "/home/ratelark/ratelark/aether_engine.py"
spec = importlib.util.spec_from_file_location("aether_engine_cf_login_test", PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

from fastapi.testclient import TestClient

results = []


def t(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL") + ": " + name)


client = TestClient(m.app, raise_server_exceptions=False)
API = "/api/strategy"
real_verify, real_configured = m.cf_access.verify, m.cf_access.configured


def setup(configured, token_ok):
    m.cf_access.configured = lambda: configured
    # mirror the real behaviour: unconfigured verify() passes everything through
    m.cf_access.verify = lambda tok: (token_ok if configured else True)

try:
    setup(True, True)
    r = client.get(API, headers={"cf-access-jwt-assertion": "valid"})
    t("configured + valid Access token: API reachable with no password or session",
      r.status_code not in (401, 303))
    r = client.get("/", headers={"cf-access-jwt-assertion": "valid"}, follow_redirects=False)
    t("configured + valid Access token: / is not redirected to /login", r.status_code != 303)

    setup(True, False)
    r = client.get(API, headers={"cf-access-jwt-assertion": "forged"})
    t("configured + invalid token: refused", r.status_code == 401)
    r = client.get(API)
    t("configured + missing token: refused", r.status_code == 401)

    setup(False, True)
    r = client.get(API)
    t("Access NOT configured: no password-free entry (fails closed)", r.status_code == 401)
    r = client.get("/", follow_redirects=False)
    t("Access NOT configured: / still redirects to /login", r.status_code == 303)

    setup(True, True)
    r = client.get(API, headers={"host": "localhost"})
    t("loopback (SSH tunnel) without a session: still refused", r.status_code == 401)
    r = client.post("/login", data={"password": ""}, headers={"host": "localhost"})
    t("password login stays disabled while DASHBOARD_PASSWORD is empty", r.status_code == 503)
finally:
    m.cf_access.verify, m.cf_access.configured = real_verify, real_configured

passed = sum(ok for _, ok in results)
print(f"\n{passed}/{len(results)} passed")
raise SystemExit(0 if passed == len(results) else 1)
