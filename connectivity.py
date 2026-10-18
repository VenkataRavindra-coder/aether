"""Live connectivity checks: GET /api/connectivity.

Answers one question for the console's Connectivity page: is every link in the chain up right now,
and if not, since when and why. The chain is you -> Cloudflare -> tunnel -> this server (engine,
dashboard) -> Telegram, GitHub, Alpaca paper, Alpaca live (plus plain internet reachability).

Read-only by design. Every probe is a harmless read: a TCP connect, a public HTTPS GET, Telegram
getMe, `git ls-remote`, Alpaca GET /v2/account, `systemctl is-active`. Nothing is written, no order
or message is sent, no secret is returned (bot tokens are scrubbed from any error text).

A background thread re-checks everything every CHECK_EVERY seconds once the page is first opened,
so "down since" is real history, not a guess. History lives in memory and resets on restart; the
page says so. Auth: the _dashboard_auth middleware in aether_engine.py covers this path.
"""
from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Depends, Request

REPO = Path(__file__).resolve().parent
CHECK_EVERY = 60
HISTORY = 30
TIMEOUT = 8
_ENV = {k: v for k, v in os.environ.items() if k != "NOTIFY_SOCKET"}

_lock = threading.Lock()
_state: dict = {}          # id -> record
_started = False
_last_cycle = 0.0


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None


def _scrub(text: str) -> str:
    text = str(text)
    for k in ("TELEGRAM_BOT_TOKEN", "APCA_API_SECRET_KEY", "APCA_LIVE_API_SECRET_KEY", "APCA_API_KEY_ID",
              "APCA_LIVE_API_KEY_ID"):
        v = os.getenv(k, "")
        if v:
            text = text.replace(v, "***")
    return text[:140]


# ---------------------------------------------------------------- probes: each returns (state, detail)
# state: "up", "down", "degraded", "off" (not configured) or "unknown" (cannot be checked here)
def p_internet():
    with socket.create_connection(("1.1.1.1", 443), timeout=4):
        pass
    return "up", "server can reach the internet"


def p_cloudflare():
    import cf_access
    url = (f"https://{cf_access.TEAM_DOMAIN}/cdn-cgi/access/certs" if cf_access.configured()
           else "https://www.cloudflare.com/cdn-cgi/trace")
    with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
        ok = 200 <= r.status < 300
    if not ok:
        return "down", "Cloudflare answered with an error"
    return "up", "Access keys endpoint reachable" if cf_access.configured() else "Cloudflare reachable (Access check not configured)"


def p_tunnel():
    try:
        r = subprocess.run(["systemctl", "is-active", "cloudflared.service"], capture_output=True, text=True,
                           timeout=5, env=_ENV)
    except FileNotFoundError:
        return "unknown", "systemctl not available here"
    st = (r.stdout or "").strip()
    return ("up", "cloudflared is active") if st == "active" else ("down", f"cloudflared is {st or 'not running'}")


def p_engine():
    from heartbeat import heartbeat_file
    import json
    hb = json.loads(heartbeat_file().read_text())
    ts = (hb.get("engine") or {}).get("ts")
    if not ts:
        return "down", "no engine heartbeat"
    age = time.time() - ts
    if age < 180:
        return "up", f"engine heartbeat {round(age)} s ago"
    if age < 900:
        return "degraded", f"engine heartbeat is {round(age / 60)} min old"
    return "down", f"engine heartbeat stale ({round(age / 60)} min)"


def _tg_get(method: str, payload: dict | None = None):
    """Read-only Telegram call. getMe proves the token and Telegram's API; getChat proves the bot can
    reach YOUR chat. Neither sends a message. (getUpdates is never called here: it would fight the
    engine's own command poller.)"""
    import json
    import notify
    token, chat = notify._config()
    req = urllib.request.Request(f"{notify._api_base()}/bot{token}/{method}", data=json.dumps(payload or {}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read())


def p_telegram():
    import notify
    token, chat = notify._config()
    if not token or not chat:
        return "off", "bot token or chat id not set"
    if not _tg_get("getMe").get("ok"):
        return "down", "Telegram rejected the bot token"
    if not _tg_get("getChat", {"chat_id": chat}).get("ok"):
        return "down", "bot cannot reach your chat (check TELEGRAM_CHAT_ID, and that you pressed Start)"
    L, now, notes, bad = notify.LINK, time.time(), [], False
    if L["send_ok"]:
        notes.append(f"last alert delivered {round((now - L['send_ok']) / 60)} min ago")
    if L["send_err"] and (not L["send_ok"] or L["send_err"] > L["send_ok"]) and now - L["send_err"] < 900:
        bad = True
        notes.append(f"sending failed {round((now - L['send_err']) / 60)} min ago ({L['send_err_text'] or 'error'})")
    if L["poll_started"]:
        if L["poll_ok"] and now - L["poll_ok"] < 120:
            notes.append(f"approvals poller answered {round(now - L['poll_ok'])} s ago")
        else:
            bad = True
            notes.append("approvals poller has not answered for over 2 min")
    dropped = getattr(notify, "_dropped", 0)
    if dropped:
        bad = True
        notes.append(f"{dropped} alert(s) dropped since restart")
    head = "bot and chat reachable"
    return ("degraded" if bad else "up"), head + ("; " + "; ".join(notes) if notes else "")


def p_github():
    env = dict(_ENV, GIT_TERMINAL_PROMPT="0")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=8")
    r = subprocess.run(["git", "ls-remote", "--heads", "origin", "main"], cwd=REPO, capture_output=True,
                       text=True, timeout=15, env=env)
    if r.returncode != 0 or not r.stdout.strip():
        why = (r.stderr or "no answer").strip().splitlines()[-1:] or ["no answer"]
        return "down", _scrub(why[0])
    remote = r.stdout.split()[0]
    loc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, timeout=5, env=env)
    local = (loc.stdout or "").strip()
    if not local:
        return "up", "GitHub reachable"
    if local == remote:
        return "up", f"GitHub reachable, server is on the latest code ({local[:7]})"
    return "up", f"GitHub reachable; GitHub has newer code ({remote[:7]}) than the server ({local[:7]})"


def _alpaca(mode):
    import dashboard_v2 as dv
    a = dv._account_summary(mode)
    if not a.get("configured"):
        return "off", "API keys not set"
    return "up", "account API answering"


def p_alpaca_paper():
    return _alpaca("paper")


def p_alpaca_live():
    return _alpaca("live")


PROBES: dict[str, Callable[[], tuple[str, str]]] = {
    "internet": p_internet, "cloudflare": p_cloudflare, "tunnel": p_tunnel, "engine": p_engine,
    "telegram": p_telegram, "github": p_github, "alpaca_paper": p_alpaca_paper, "alpaca_live": p_alpaca_live,
}


def _run_one(pid: str, fn: Callable) -> dict:
    t0 = time.time()
    try:
        state, detail = fn()
    except urllib.error.HTTPError as e:
        state, detail = "down", _scrub(f"HTTP {e.code} {e.reason}")
    except Exception as e:  # a probe must never raise into the page
        state, detail = "down", _scrub(f"{type(e).__name__}: {e}")
    return {"id": pid, "state": state, "detail": detail, "ms": round((time.time() - t0) * 1000), "at": time.time()}


def _record(res: dict) -> None:
    with _lock:
        r = _state.setdefault(res["id"], {"history": deque(maxlen=HISTORY), "state_since": res["at"], "last_up": None,
                                          "state": None})
        if r["state"] != res["state"]:
            r["state_since"] = res["at"]
        r.update(state=res["state"], detail=res["detail"], ms=res["ms"], at=res["at"])
        if res["state"] in ("up", "degraded"):
            r["last_up"] = res["at"]
        r["history"].append(res["state"])


def run_once(probes: dict | None = None) -> None:
    global _last_cycle
    probes = probes or PROBES
    with ThreadPoolExecutor(max_workers=len(probes)) as ex:
        for res in ex.map(lambda kv: _run_one(*kv), probes.items()):
            _record(res)
    _last_cycle = time.time()


def _loop():
    while True:
        try:
            run_once()
        except Exception:
            pass
        time.sleep(CHECK_EVERY)


def ensure_started() -> None:
    global _started
    if os.getenv("CONNECTIVITY_BG", "1") == "0":
        return
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, name="aether-connectivity", daemon=True).start()


def snapshot(now: float | None = None) -> dict:
    now = now or time.time()
    with _lock:
        probes = {}
        for pid, r in _state.items():
            bad = r["state"] in ("down",)
            probes[pid] = {"state": r["state"], "detail": r["detail"], "ms": r["ms"], "checked_at": _iso(r["at"]),
                           "age_s": round(now - r["at"]), "since": _iso(r["state_since"]),
                           "in_state_s": round(now - r["state_since"]), "last_up": _iso(r["last_up"]),
                           "down_for_s": round(now - r["state_since"]) if bad else None,
                           "history": list(r["history"])}
    down = sorted(k for k, v in probes.items() if v["state"] == "down")
    return {"generated_at": _iso(now), "check_every_s": CHECK_EVERY, "history_note": "kept in memory; resets when the engine restarts",
            "probes": probes, "down": down}


def build_router(auth_dep: Callable) -> APIRouter:
    r = APIRouter(dependencies=[Depends(auth_dep)])

    @r.get("/api/connectivity")
    def connectivity(request: Request):
        import cf_access
        ensure_started()
        if not _state:
            run_once()
        out = snapshot()
        h = request.headers
        out["you"] = {"via_cloudflare": bool(h.get("cf-access-jwt-assertion") or h.get("cf-ray")),
                      "access_jwt_validation": cf_access.configured()}
        return out

    return r
