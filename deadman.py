#!/usr/bin/env python3
"""AETHER dead-man monitor: your own healthchecks.io.

Run this on a DIFFERENT machine than the trading server (a second small VPS, an always-on home or office
PC, a Raspberry Pi). The trading server pings it every minute (the engine already does: set HEALTHCHECK_URL
in .env to  https://<this-host>/ping/<secret> ). If the pings stop, this program messages you on Telegram.
That is the only way to learn that the whole server, its internet link or its tunnel died: a dead machine
cannot report itself.

URLs (same shape as healthchecks.io, so nothing on the server needs changing):
  GET|POST /ping/<secret>          "I am alive"
  GET|POST /ping/<secret>/start    accepted, counts as alive
  GET|POST /ping/<secret>/fail     "I am broken": alerts immediately
  GET      /status/<secret>        JSON: is it up, seconds since the last ping (used by the dashboard)
  GET      /healthz                "ok" (for your own checks of this monitor)

Alerts (Telegram, using the same bot as AETHER, or any bot):
  - no ping for GRACE seconds         -> "no check-in for N min"        (once, then a reminder every REMIND s)
  - a ping arrives after an outage    -> "back after N min"
  - /fail                             -> "reported a failure"
  - once a day                        -> "monitor alive" so that silence tells you THIS monitor died
Undelivered messages stay queued on disk and are retried until Telegram accepts them.

Configuration (environment):
  DEADMAN_SECRET            required: the secret in the ping URL (long and random; e.g. python3 -c "import secrets;print(secrets.token_urlsafe(24))")
  DEADMAN_CHECKS            optional extra checks, "name=secret,name2=secret2" (default check is named "aether")
  DEADMAN_GRACE_S           seconds without a ping before alerting (default 240; the engine pings every 60)
  DEADMAN_REMIND_S          repeat the "still down" reminder this often (default 21600 = 6 h; 0 = never)
  DEADMAN_DAILY_HOUR_UTC    hour of the daily "monitor alive" message (default 5; -1 = off)
  DEADMAN_HOST / DEADMAN_PORT   listen address (default 127.0.0.1:8099; put a tunnel or TLS proxy in front)
  DEADMAN_STATE             state file (default ./deadman_state.json)
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   where alerts go
Python standard library only.
"""
from __future__ import annotations

import hmac
import json
import os
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _dur(s: float) -> str:
    s = int(max(0, s))
    return f"{s} s" if s < 120 else f"{round(s / 60)} min" if s < 7200 else f"{round(s / 3600, 1)} h"


class Monitor:
    def __init__(self, checks: dict, grace_s: float = 240, remind_s: float = 21600, daily_hour: int = 5,
                 state_path: Path | None = None, send=None, clock=time.time):
        self.checks = dict(checks)                       # name -> secret
        self.grace, self.remind, self.daily_hour = grace_s, remind_s, daily_hour
        self.path, self._send, self.now = state_path, send or (lambda text: True), clock
        self.lock = threading.Lock()
        self.s = {"checks": {}, "outbox": [], "daily": ""}
        self._load()
        for n in self.checks:
            self.s["checks"].setdefault(n, {"last_ping": None, "status": "new", "down_since": None, "last_alert": None,
                                           "created": self.now(), "reason": ""})

    # ------------------------------------------------------------------ persistence
    def _load(self):
        try:
            if self.path and Path(self.path).exists():
                self.s.update(json.loads(Path(self.path).read_text()))
        except Exception:
            pass

    def _save(self):
        if not self.path:
            return
        try:
            tmp = Path(str(self.path) + ".tmp")
            tmp.write_text(json.dumps(self.s))
            tmp.replace(self.path)
        except Exception:
            pass

    # ------------------------------------------------------------------ events
    def _queue(self, text: str):
        self.s["outbox"].append(text)

    def name_for(self, secret: str):
        found = None
        for n, sec in self.checks.items():             # compare against all, constant time, no early exit
            if hmac.compare_digest(sec.encode(), secret.encode()):
                found = n
        return found

    def ping(self, secret: str, kind: str = "") -> str | None:
        name = self.name_for(secret)
        if name is None:
            return None
        with self.lock:
            c, now = self.s["checks"][name], self.now()
            if kind == "fail":
                if c["status"] != "down" or c.get("reason") != "reported failure":
                    self._queue(f"\U0001F534 {name}: the server reported a failure (it pinged /fail). Check it now.")
                c.update(status="down", down_since=c["down_since"] or now, last_alert=now, reason="reported failure")
                c["last_ping"] = now
            else:
                if c["status"] == "down":
                    self._queue(f"\U0001F7E2 {name} is back after {_dur(now - (c['down_since'] or now))}.")
                c.update(status="up", down_since=None, last_ping=now, last_alert=None, reason="")
            self._save()
        return name

    def tick(self):
        """Evaluate every check, queue alerts, then try to deliver the outbox. Safe to call often."""
        with self.lock:
            now = self.now()
            for name, c in self.s["checks"].items():
                if c["status"] == "up" and c["last_ping"] and now - c["last_ping"] > self.grace:
                    c.update(status="down", down_since=c["last_ping"], last_alert=now, reason="no check-in")
                    self._queue(f"\U0001F534 {name}: no check-in for {_dur(now - c['last_ping'])}. The server, its internet link, "
                                f"its tunnel or the engine may be down. Check the Hetzner console.")
                elif c["status"] == "new" and now - c["created"] > self.grace * 3:
                    c.update(status="down", down_since=c["created"], last_alert=now, reason="never checked in")
                    self._queue(f"\U0001F7E0 {name}: no check-in has ever arrived. Is HEALTHCHECK_URL set on the server, and can it reach this monitor?")
                elif c["status"] == "down" and self.remind and c["last_alert"] and now - c["last_alert"] >= self.remind:
                    c["last_alert"] = now
                    self._queue(f"\U0001F534 {name} is still down ({_dur(now - (c['down_since'] or now))}, {c.get('reason') or 'no check-in'}).")
            d = datetime.fromtimestamp(now, timezone.utc)
            if self.daily_hour >= 0 and d.hour == self.daily_hour and self.s["daily"] != d.strftime("%Y-%m-%d"):
                self.s["daily"] = d.strftime("%Y-%m-%d")
                parts = []
                for n, c in self.s["checks"].items():
                    parts.append(f"{n}: " + ("no ping yet" if not c["last_ping"] else f"{c['status']}, last check-in {_dur(now - c['last_ping'])} ago"))
                self._queue("\U0001F4A4 Monitor alive. " + "; ".join(parts))
            sent = 0
            while self.s["outbox"] and sent < 5:
                try:
                    ok = self._send(self.s["outbox"][0])
                except Exception:
                    ok = False
                if not ok:
                    break                              # keep it queued; retried on the next tick
                self.s["outbox"].pop(0)
                sent += 1
            self._save()

    def status(self, secret: str):
        name = self.name_for(secret)
        if name is None:
            return None
        with self.lock:
            c, now = self.s["checks"][name], self.now()
            return {"name": name, "status": c["status"], "up": c["status"] == "up", "last_ping_age_s": None if not c["last_ping"] else round(now - c["last_ping"]),
                    "down_for_s": round(now - c["down_since"]) if c["status"] == "down" and c["down_since"] else None,
                    "grace_s": self.grace, "queued_alerts": len(self.s["outbox"])}


def telegram_sender(token: str, chat: str, base: str = "https://api.telegram.org"):
    def send(text: str) -> bool:
        if not token or not chat:
            return False
        req = urllib.request.Request(f"{base}/bot{token}/sendMessage", data=json.dumps({"chat_id": chat, "text": text[:3900]}).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return bool(json.loads(r.read()).get("ok"))
        except Exception:
            return False
    return send


def make_handler(mon: Monitor):
    class H(BaseHTTPRequestHandler):
        server_version = "deadman"
        def log_message(self, *a):                      # never log paths: they contain the secret
            pass

        def _reply(self, code: int, body: bytes = b"OK", ctype: str = "text/plain"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _route(self):
            parts = [p for p in self.path.split("?")[0].split("/") if p]
            if parts == ["healthz"]:
                return self._reply(200)
            if len(parts) >= 2 and parts[0] == "ping" and (len(parts) == 2 or parts[2] in ("fail", "start")):
                name = mon.ping(parts[1], parts[2] if len(parts) == 3 else "")
                return self._reply(200) if name else self._reply(404, b"not found")
            if len(parts) == 2 and parts[0] == "status":
                st = mon.status(parts[1])
                return self._reply(200, json.dumps(st).encode(), "application/json") if st else self._reply(404, b"not found")
            return self._reply(404, b"not found")
        do_GET = do_POST = do_HEAD = _route
    return H


def main() -> int:
    secret = os.environ.get("DEADMAN_SECRET", "").strip()
    checks = {"aether": secret} if secret else {}
    for item in os.environ.get("DEADMAN_CHECKS", "").split(","):
        if "=" in item:
            n, v = item.split("=", 1)
            if n.strip() and v.strip():
                checks[n.strip()] = v.strip()
    if not checks:
        print("Set DEADMAN_SECRET (a long random string) first. See the header of this file.")
        return 2
    if any(len(v) < 16 for v in checks.values()):
        print("Every secret must be at least 16 characters: it is the only protection on the ping URL.")
        return 2
    mon = Monitor(checks, grace_s=float(os.environ.get("DEADMAN_GRACE_S", 240)), remind_s=float(os.environ.get("DEADMAN_REMIND_S", 21600)),
                  daily_hour=int(os.environ.get("DEADMAN_DAILY_HOUR_UTC", 5)), state_path=Path(os.environ.get("DEADMAN_STATE", "deadman_state.json")),
                  send=telegram_sender(os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", ""),
                                       os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")))
    def loop():
        while True:
            try:
                mon.tick()
            except Exception:
                pass
            time.sleep(15)
    threading.Thread(target=loop, daemon=True).start()
    host, port = os.environ.get("DEADMAN_HOST", "127.0.0.1"), int(os.environ.get("DEADMAN_PORT", 8099))
    print(f"deadman listening on {host}:{port}; checks: {', '.join(checks)}; grace {mon.grace:.0f}s", flush=True)
    ThreadingHTTPServer((host, port), make_handler(mon)).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
