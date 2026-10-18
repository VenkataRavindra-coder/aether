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

import json
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

EVENTS_FILE = Path(os.getenv("AETHER_HOME") or REPO) / "connectivity_events.jsonl"
NAMES = {"internet": "Internet", "cloudflare": "Cloudflare", "tunnel": "Cloudflare tunnel", "engine": "Engine",
         "telegram": "Telegram", "monitor": "Outside monitor", "github": "GitHub", "alpaca_paper": "Alpaca paper", "alpaca_live": "Alpaca live"}
FIX = {"tunnel": "On the server: sudo systemctl restart cloudflared",
       "engine": "On the server: sudo systemctl restart ratelark",
       "internet": "Check the Hetzner console for the server's network",
       "telegram": "Check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env",
       "monitor": "Check that deadman.py is running on the other machine and that HEALTHCHECK_URL in .env points to it",
       "github": "On the server: cd ~/ratelark; git ls-remote origin main",
       "alpaca_paper": "Check the paper keys in .env and status.alpaca.markets",
       "alpaca_live": "Check the live keys in .env.live and status.alpaca.markets",
       "cloudflare": "Check cloudflarestatus.com"}
DOWN_AFTER = 2             # consecutive failed checks before a link counts as down for alerts (no flapping)
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
    """Read-only Telegram call through notify._call, so Telegram's reply lands in the response log.
    getMe proves the token and Telegram's API; getChat proves the bot can reach YOUR chat. Neither sends a
    message. (getUpdates is never called here: it would fight the engine's own command poller.)"""
    import notify
    return notify._call(method, payload or {}, timeout=TIMEOUT)


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


def p_monitor():
    """The outside dead-man monitor (deadman.py on another machine). Its /status answers prove that it is alive,
    that the server can reach it, and that our pings are arriving. A third-party URL (healthchecks.io) has no
    status API, so it is reported as 'unknown' instead of guessed."""
    url = (os.getenv("HEALTHCHECK_URL") or os.getenv("HEALTHCHECK_PING_URL") or "").strip()
    if not url:
        return "off", "HEALTHCHECK_URL not set: nothing outside the server will notice if it dies"
    if "/ping/" not in url or "hc-ping.com" in url:
        return "unknown", "third-party monitor: its answer cannot be read from here"
    base, _, rest = url.partition("/ping/")
    secret = rest.split("/")[0].split("?")[0]
    import json as _j
    req = urllib.request.Request(f"{base}/status/{secret}", headers={"User-Agent": "AETHER-ops/1.0 (+https://ops.ratelark.app)"})  # Cloudflare 403s the default Python agent
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        st = _j.loads(r.read())
    age = st.get("last_ping_age_s")
    if age is None:
        return "degraded", "monitor reachable but has not received a ping yet"
    if not st.get("up"):
        return "down", f"monitor says the server is down ({st.get('status')})"
    if age > st.get("grace_s", 240) * 0.75:
        return "degraded", f"monitor reachable, but our last ping arrived {age} s ago"
    return "up", f"monitor reachable, last ping seen {age} s ago"


PROBES: dict[str, Callable[[], tuple[str, str]]] = {
    "internet": p_internet, "cloudflare": p_cloudflare, "tunnel": p_tunnel, "engine": p_engine,
    "telegram": p_telegram, "monitor": p_monitor, "github": p_github, "alpaca_paper": p_alpaca_paper, "alpaca_live": p_alpaca_live,
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


def _event(pid: str, ev: str, detail: str, since: float, at: float) -> None:
    """Down/up history that survives restarts (connectivity_events.jsonl, last 300 lines kept)."""
    try:
        with open(EVENTS_FILE, "a") as f:
            f.write(json.dumps({"ts": round(at, 1), "id": pid, "ev": ev, "detail": detail[:140], "since": round(since, 1)}) + "\n")
        if EVENTS_FILE.stat().st_size > 100_000:
            EVENTS_FILE.write_text("\n".join(EVENTS_FILE.read_text().splitlines()[-300:]) + "\n")
    except Exception:
        pass


def recent_events(n: int = 20) -> list:
    try:
        return [json.loads(x) for x in EVENTS_FILE.read_text().splitlines()[-n:]][::-1]
    except Exception:
        return []


def _alert(pid: str, text: str, key: str) -> None:
    if os.getenv("CONNECTIVITY_ALERTS", "1") == "0":
        return
    try:
        import notify
        notify.send(text, key=key, cooldown=300)
    except Exception:
        pass


def _restore(pid: str):
    """If the last recorded event for this link was 'down', the down period began then, even across a restart."""
    for e in recent_events(300):
        if e.get("id") == pid:
            return e if e.get("ev") == "down" else None
    return None


def _duration(s: float) -> str:
    s = int(s)
    return f"{s}s" if s < 90 else f"{round(s / 60)} min" if s < 5400 else f"{round(s / 3600, 1)} h"


def _record(res: dict) -> None:
    pid, now, notify_after = res["id"], res["at"], None
    with _lock:
        new = pid not in _state
        r = _state.setdefault(pid, {"history": deque(maxlen=HISTORY), "state_since": now, "last_up": None, "state": None,
                                    "streak": 0, "alerted": False, "alert_since": None})
        if new and res["state"] == "down":
            prev = _restore(pid)
            if prev:
                r["state_since"], r["alerted"], r["alert_since"] = prev["since"], True, prev["since"]
        if r["state"] != res["state"] and not (new and r["alerted"]):
            r["state_since"] = now
        r.update(state=res["state"], detail=res["detail"], ms=res["ms"], at=now)
        if res["state"] in ("up", "degraded"):
            r["last_up"] = now
        r["history"].append(res["state"])
        if res["state"] == "down":
            r["streak"] += 1
            if r["streak"] >= DOWN_AFTER and not r["alerted"]:
                r["alerted"], r["alert_since"] = True, r["state_since"]
                notify_after = ("down", res["detail"], r["state_since"])
        elif res["state"] in ("up", "degraded", "off"):
            r["streak"] = 0
            if r["alerted"]:
                notify_after = ("up", res["detail"], r["alert_since"])
                r["alerted"], r["alert_since"] = False, None
    if notify_after:                      # outside the lock: file and network work
        ev, detail, since = notify_after
        _event(pid, ev, detail, since, now)
        name = NAMES.get(pid, pid)
        if ev == "down" and pid != "telegram":        # Telegram cannot announce its own outage; it reports on recovery
            _alert(pid, f"\U0001F534 AETHER: {name} is disconnected. {detail}\nFix: {FIX.get(pid, 'see the Connectivity page')}", f"conn-down-{pid}")
        elif ev == "up":
            _alert(pid, f"\U0001F7E2 AETHER: {name} is back after {_duration(now - since)}." +
                   (" (This alert was held until Telegram came back.)" if pid == "telegram" else ""), f"conn-up-{pid}")


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


def _tg_log() -> dict:
    """What Telegram last sent back to this server (newest first) plus counts over the last 100 calls."""
    import notify
    allr = notify.recent_responses(100)
    sent = [r for r in allr if r.get("m") == "sendMessage"]
    timed = [r["ms"] for r in allr if r.get("ms") is not None and r.get("m") != "getUpdates"]
    return {"rows": allr[:30], "sent_ok": sum(1 for r in sent if r.get("ok")), "sent_failed": sum(1 for r in sent if not r.get("ok")),
            "received": sum(1 for r in allr if r.get("m") == "received"), "avg_ms": round(sum(timed) / len(timed)) if timed else None}


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
        out["events"] = recent_events(20)
        out["telegram_log"] = _tg_log()
        out["you"] = {"via_cloudflare": bool(h.get("cf-access-jwt-assertion") or h.get("cf-ray")),
                      "access_jwt_validation": cf_access.configured()}
        return out

    return r
