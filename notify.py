"""Telegram notifications, on-demand status commands and VM metrics for AETHER.

Design rules: sending never blocks or breaks trading (queued, background thread, every error
swallowed and logged); the bot token is never logged; only the configured chat may issue
commands; and repeated alerts are de-duplicated with a per-key cooldown.
"""
import json
import logging
import os
import queue
import shutil
import threading
import time
import urllib.error
import urllib.request

logger = logging.getLogger("AETHER_NOTIFY")

MAX_LEN = 4000
SEND_GAP_SECONDS = 1.1

_queue = queue.Queue(maxsize=200)
_worker_started = False
_worker_lock = threading.Lock()
_last_sent = {}
_dedupe_lock = threading.Lock()
STOP = threading.Event()


def _api_base() -> str:
    return os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org")


def _config():
    return os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")


def enabled() -> bool:
    token, chat = _config()
    return bool(token and chat)


def _scrub(text: str) -> str:
    token, _ = _config()
    return text.replace(token, "***") if token else text


def _call(method: str, payload: dict, timeout: float = 15):
    token, _ = _config()
    req = urllib.request.Request(
        f"{_api_base()}/bot{token}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _worker():
    while not STOP.is_set():
        try:
            text = _queue.get(timeout=1)
        except queue.Empty:
            continue
        _, chat = _config()
        for attempt in range(3):
            try:
                _call("sendMessage", {"chat_id": chat, "text": text[:MAX_LEN],
                                      "disable_web_page_preview": True})
                break
            except urllib.error.HTTPError as e:
                wait = 5 * (attempt + 1)
                if e.code == 429:
                    try:
                        wait = json.loads(e.read()).get("parameters", {}).get("retry_after", wait)
                    except Exception:
                        pass
                logger.warning(f"Telegram send failed (HTTP {e.code}); retrying in {wait}s")
                STOP.wait(wait)
            except Exception as e:
                logger.warning(f"Telegram send failed: {_scrub(str(e))}")
                STOP.wait(5 * (attempt + 1))
        STOP.wait(SEND_GAP_SECONDS)


def send(text: str, key: str = None, cooldown: float = 0) -> bool:
    """Queue a message. With `key` and `cooldown`, the same key is sent at most once per
    cooldown window. Returns False if disabled, de-duplicated, or the queue is full."""
    global _worker_started
    if not enabled():
        return False
    if key and cooldown:
        now = time.time()
        with _dedupe_lock:
            if now - _last_sent.get(key, 0) < cooldown:
                return False
            _last_sent[key] = now
    with _worker_lock:
        if not _worker_started:
            threading.Thread(target=_worker, daemon=True, name="aether-telegram").start()
            _worker_started = True
    try:
        _queue.put_nowait(text)
        return True
    except queue.Full:
        logger.warning("Telegram queue full; dropping message")
        return False


def start_command_poller(handler):
    """Long-polls for commands like /status. `handler(command) -> str | None`.
    Messages from any chat other than TELEGRAM_CHAT_ID are ignored."""
    if not enabled():
        return

    def loop():
        offset = 0
        try:
            latest = _call("getUpdates", {"offset": -1, "timeout": 0}, timeout=15).get("result", [])
            if latest:
                offset = latest[-1]["update_id"] + 1
        except Exception as e:
            logger.warning(f"Telegram poller init failed: {_scrub(str(e))}")
        while not STOP.is_set():
            try:
                _, chat = _config()
                updates = _call("getUpdates", {"offset": offset, "timeout": 25,
                                               "allowed_updates": ["message"]}, timeout=40).get("result", [])
                for u in updates:
                    offset = u["update_id"] + 1
                    msg = u.get("message") or {}
                    if str((msg.get("chat") or {}).get("id")) != str(chat):
                        continue
                    text = (msg.get("text") or "").strip()
                    if not text.startswith("/"):
                        continue
                    cmd = text.split()[0].split("@")[0].lower()
                    try:
                        reply = handler(cmd)
                    except Exception as e:
                        logger.exception("Command handler failed")
                        reply = f"Command failed: {type(e).__name__}"
                    if reply:
                        send(reply)
            except Exception as e:
                logger.warning(f"Telegram poll error: {_scrub(str(e))}")
                STOP.wait(10)

    threading.Thread(target=loop, daemon=True, name="aether-telegram-poll").start()


def fmt_duration(seconds) -> str:
    seconds = int(seconds or 0)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return (f"{d}d " if d else "") + (f"{h}h " if h or d else "") + f"{m}m"


def vm_metrics() -> dict:
    """Load, memory, disk and uptime of this machine. Missing values are simply omitted
    (e.g. /proc does not exist on macOS)."""
    m = {"cpus": os.cpu_count() or 1}
    try:
        with open("/proc/loadavg") as f:
            m["load1"], m["load5"], m["load15"] = (float(x) for x in f.read().split()[:3])
    except Exception:
        pass
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0])
        m["mem_used_pct"] = round((1 - info["MemAvailable"] / info["MemTotal"]) * 100, 1)
        m["mem_total_mb"] = round(info["MemTotal"] / 1024)
    except Exception:
        pass
    try:
        with open("/proc/uptime") as f:
            m["vm_uptime_s"] = float(f.read().split()[0])
    except Exception:
        pass
    try:
        du = shutil.disk_usage("/")
        m["disk_used_pct"] = round(du.used / du.total * 100, 1)
        m["disk_free_gb"] = round(du.free / 1e9, 1)
    except Exception:
        pass
    m["reboot_required"] = os.path.exists("/var/run/reboot-required")
    return m
