"""Engine heartbeat. Call beat() once per engine loop. Never raises."""
from __future__ import annotations

import json
import time

from safety_guard import atomic_write, home


def heartbeat_file():
    return home() / "heartbeat.json"


def beat(component: str = "engine", **info) -> None:
    try:
        p = heartbeat_file()
        data = json.loads(p.read_text()) if p.exists() else {}
        data[component] = {"ts": time.time(), **info}
        atomic_write(p, json.dumps(data))
    except Exception:
        pass  # a heartbeat must never break the engine
