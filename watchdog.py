"""External watchdog (systemd timer, every 5 min). Runs OUTSIDE aether.service.
Alerts on: service down, stale heartbeat, stale live risk mark.
Optional off-VM dead-man: set HEALTHCHECK_URL (e.g. healthchecks.io)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import date, datetime

import aether_ops
import safety_guard as g
from heartbeat import heartbeat_file

REALERT_SEC = 3600


def assess(now: float, heartbeat: dict, service_active: bool, ledger: dict,
           stale_min: float = 15, mark_age_days: int = 4, today: date | None = None) -> list[str]:
    problems = []
    if not service_active:
        problems.append(f"{_service_name()} is NOT active")
    hb = heartbeat.get("engine", {}).get("ts")
    if hb is None:
        problems.append("no engine heartbeat recorded")
    elif now - hb > stale_min * 60:
        problems.append(f"engine heartbeat stale {int((now - hb) / 60)} min")
    last = ledger.get("last_mark")
    if last:
        today = today or datetime.now(g.ET).date()
        age = (today - date.fromisoformat(last["date"])).days
        if age > mark_age_days:
            problems.append(f"live risk mark stale ({last['date']}); breakers blind")
    return problems


def _service_name() -> str:
    """The engine's systemd unit. On Hetzner it is ratelark.service (set AETHER_SERVICE in .env)."""
    return os.environ.get("AETHER_SERVICE", "aether.service").strip() or "aether.service"


def _service_active() -> bool:
    r = subprocess.run(["systemctl", "is-active", "--quiet", _service_name()])
    return r.returncode == 0


def _live_positions() -> str:
    try:
        pos = aether_ops.alpaca_live_get("/v2/positions")
        if not pos:
            return "live positions: none"
        return "LIVE POSITIONS (no stop orders; nothing manages them while the engine is down): " + ", ".join(
            f"{p['symbol']} {p['qty']}" for p in pos)
    except Exception as e:
        return f"live positions: unknown ({e})"


def _healthcheck_url():
    """Off-VM dead-man URL. HEALTHCHECK_URL is the documented name (test plan RD4); the engine and
    setup_telegram.sh historically wrote HEALTHCHECK_PING_URL. Accept either so a server set up
    with only one of them still has an off-VM alarm when the whole box goes down."""
    return os.environ.get("HEALTHCHECK_URL") or os.environ.get("HEALTHCHECK_PING_URL")


def main() -> int:
    aether_ops.load_env()
    hb_path = heartbeat_file()
    hb = json.loads(hb_path.read_text()) if hb_path.exists() else {}
    problems = assess(time.time(), hb, _service_active(), g.load_ledger(),
                      stale_min=float(os.environ.get("WATCHDOG_STALE_MIN", 15)))
    st_path = g.home() / "watchdog_state.json"
    st = json.loads(st_path.read_text()) if st_path.exists() else {}
    now = time.time()
    key = "|".join(problems)
    if problems:
        if key != st.get("key") or now - st.get("alerted", 0) > REALERT_SEC:
            aether_ops.telegram("AETHER WATCHDOG ALERT\n- " + "\n- ".join(problems) + "\n" + _live_positions())
            st = {"key": key, "alerted": now}
        aether_ops.ping(_healthcheck_url(), fail=True)
    else:
        if st.get("key"):
            aether_ops.telegram("AETHER watchdog: recovered, all checks OK")
        st = {"key": "", "alerted": 0}
        aether_ops.ping(_healthcheck_url())
    g.atomic_write(st_path, json.dumps(st))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
