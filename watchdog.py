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
        problems.append("aether.service is NOT active")
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


def _service_active() -> bool:
    r = subprocess.run(["systemctl", "is-active", "--quiet", "aether.service"])
    return r.returncode == 0


def _live_positions() -> str:
    try:
        pos = aether_ops.alpaca_live_get("/v2/positions")
        if not pos:
            return "live positions: none"
        return "LIVE POSITIONS UNPROTECTED (engine-managed stops): " + ", ".join(
            f"{p['symbol']} {p['qty']}" for p in pos)
    except Exception as e:
        return f"live positions: unknown ({e})"


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
        aether_ops.ping(os.environ.get("HEALTHCHECK_URL"), fail=True)
    else:
        if st.get("key"):
            aether_ops.telegram("AETHER watchdog: recovered, all checks OK")
        st = {"key": "", "alerted": 0}
        aether_ops.ping(os.environ.get("HEALTHCHECK_URL"))
    g.atomic_write(st_path, json.dumps(st))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
