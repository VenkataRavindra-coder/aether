"""Offline test proving the actual bug behind A4.6 and that send_sync() fixes it: send()
returns before the network call happens (fine for the long-running engine, fatal for a one-shot
script whose process exits right after calling it -- the daemon worker never gets to run).
No real network calls (notify._call is stubbed). Run: python3 test_notify_sync.py
"""
import os
import time
import threading

os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
os.environ["TELEGRAM_CHAT_ID"] = "12345"

import notify

results = []


def t(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL") + ": " + name)


# Stub _call to record when it actually runs, with an artificial delay so a race between
# "did send() return before the network call happened" and "did it happen after" is unambiguous.
call_log = []
call_lock = threading.Lock()


def fake_call(method, payload, timeout=15):
    time.sleep(0.3)  # stand-in for real network latency
    with call_lock:
        call_log.append(payload["text"])
    return {"ok": True}


notify._call = fake_call

# --- the bug: send() returns before the message is actually transmitted ---
call_log.clear()
ok = notify.send("async message", key=None)
t("send() reports queued (True)", ok is True)
t("...but the network call has NOT happened yet at the moment send() returns "
  "(this is exactly what kills a one-shot script: it exits right here)",
  "async message" not in call_log)
time.sleep(0.6)  # let the background worker actually catch up, for its own sake
t("...it does eventually get sent, once the background worker has time to run",
  "async message" in call_log)

# --- the fix: send_sync() only returns once the message is actually transmitted ---
call_log.clear()
ok = notify.send_sync("sync message")
t("send_sync() reports success", ok is True)
t("by the time send_sync() returns, the network call has ALREADY happened -- a one-shot "
  "script is safe to exit immediately afterward",
  "sync message" in call_log)

# --- send_sync respects the enabled()/disabled gate like send() does ---
os.environ["TELEGRAM_BOT_TOKEN"] = ""
t("send_sync() returns False when Telegram isn't configured, not an exception",
  notify.send_sync("should not send") is False)
os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"

# --- send_sync surfaces a send failure as False instead of raising ---
def failing_call(method, payload, timeout=15):
    raise RuntimeError("simulated network failure")


notify._call = failing_call
t("send_sync() returns False (not an exception) when the network call fails",
  notify.send_sync("doomed message") is False)

fails = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(fails)}/{len(results)} passed")
if fails:
    print("FAILED:", fails)
    raise SystemExit(1)
