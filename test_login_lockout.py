"""Offline, endpoint-level tests for the /login per-IP rate limit. Spins up the real FastAPI
app (with every background loop disabled) via TestClient -- no network, no real credentials.
Run: python3 test_login_lockout.py
"""
import os
import importlib.util

os.environ.update(
    LIVE_TRADING="false", US_LIVE="false", US_EXECUTE="false", US_ENABLED="false",
    STRATEGY_ENABLED="false", TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID="",
    WEBHOOK_SECRET="test-webhook-secret", DASHBOARD_PASSWORD="correct-horse-battery-staple",
    APCA_API_KEY_ID="", APCA_API_SECRET_KEY="", APCA_LIVE_API_KEY_ID="", APCA_LIVE_API_SECRET_KEY="",
)

PATH = "/home/azureuser/aether_engine.py"
spec = importlib.util.spec_from_file_location("aether_engine_login_test", PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

from fastapi.testclient import TestClient

results = []


def t(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL") + ": " + name)


client = TestClient(m.app)
HDR = {"cf-connecting-ip": "203.0.113.5"}  # fixed source IP for the lockout tests

# reset any state a previous run in this process left behind
m._login_attempts.clear()

# Attempts 1-5: each wrong password is rejected normally (401) -- the 5th is also the one
# that actually arms the lockout for whatever comes next, but it still reports 401 itself.
for i in range(5):
    r = client.post("/login", data={"password": "wrong"}, headers=HDR, follow_redirects=False)
    t(f"wrong-password attempt {i+1}/5 returns 401, not locked yet", r.status_code == 401)

# Attempt 6: the lockout armed by attempt 5 now applies, even to a WRONG password.
r = client.post("/login", data={"password": "wrong"}, headers=HDR, follow_redirects=False)
t("the 6th attempt (over the threshold) is locked out", r.status_code == 429)

r = client.post("/login", data={"password": "correct-horse-battery-staple"}, headers=HDR, follow_redirects=False)
t("locked out even with the CORRECT password now", r.status_code == 429)
t("the lockout reports a retry-after style wait time", "s." in r.json().get("detail", ""))

OTHER_HDR = {"cf-connecting-ip": "198.51.100.9"}
r = client.post("/login", data={"password": "correct-horse-battery-staple"}, headers=OTHER_HDR, follow_redirects=False)
t("a different source IP is not affected by another IP's lockout", r.status_code == 303)

m._login_attempts.clear()
client.post("/login", data={"password": "wrong"}, headers=HDR, follow_redirects=False)
client.post("/login", data={"password": "wrong"}, headers=HDR, follow_redirects=False)
r = client.post("/login", data={"password": "correct-horse-battery-staple"}, headers=HDR, follow_redirects=False)
t("a correct password clears that IP's failure count", r.status_code == 303 and HDR["cf-connecting-ip"] not in m._login_attempts)

fails = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(fails)}/{len(results)} passed")
if fails:
    print("FAILED:", fails)
    raise SystemExit(1)
