"""Shared helpers for ops scripts: .env loading, Telegram, read-only Alpaca live."""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

from safety_guard import home


def load_env() -> None:
    """Loads AETHER_HOME/.env, then AETHER_HOME/.env.live if present. Existing variables win.

    .env.live holds ONLY the live Alpaca keys (APCA_LIVE_API_KEY_ID / APCA_LIVE_API_SECRET_KEY),
    mode 600, so the six systemd units that load the shared .env (backup, daily, health, stress,
    watchdog) no longer carry real-money credentials. Scripts that need them -- risk_mark.py,
    risk_admin.py -- get them through this loader; aether.service loads it as a second
    EnvironmentFile. Keys still sitting in .env keep working exactly as before."""
    for name in (".env", ".env.live"):
        _load_file(home() / name)


def _load_file(p) -> None:
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def telegram(text: str) -> bool:
    tok = os.environ.get(os.environ.get("GUARD_TG_TOKEN_VAR", "TELEGRAM_BOT_TOKEN"))
    chat = os.environ.get(os.environ.get("GUARD_TG_CHAT_VAR", "TELEGRAM_CHAT_ID"))
    if not tok or not chat:
        print("[telegram not configured]", text)
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data, timeout=10)
        return True
    except Exception as e:
        print("[telegram failed]", e, text)
        return False


def alpaca_live_get(path: str):
    req = urllib.request.Request(
        "https://api.alpaca.markets" + path,
        headers={"APCA-API-KEY-ID": os.environ["APCA_LIVE_API_KEY_ID"],
                 "APCA-API-SECRET-KEY": os.environ["APCA_LIVE_API_SECRET_KEY"]})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def ping(url: str | None, fail: bool = False) -> None:
    if not url:
        return
    try:
        urllib.request.urlopen(url.rstrip("/") + ("/fail" if fail else ""), timeout=10)
    except Exception:
        pass
