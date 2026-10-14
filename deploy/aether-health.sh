#!/usr/bin/env bash
# Restarts the engine if its local API stops answering (hung process, deadlock).
# Skips the check for 90 s after a (re)start so slow startups are not killed.
# Service name: AETHER_SERVICE from .env (ratelark.service on Hetzner), default aether.service.
SVC="${AETHER_SERVICE:-aether.service}"
PORT="${AETHER_PORT:-8000}"
started=$(systemctl show "$SVC" -p ActiveEnterTimestampMonotonic --value)
now=$(cut -d. -f1 /proc/uptime)
if [ -n "$started" ] && [ "$started" != "0" ] && [ $((now - started / 1000000)) -lt 90 ]; then exit 0; fi
if ! curl -sf -m 15 -o /dev/null "http://127.0.0.1:${PORT}/openapi.json"; then
  logger -t aether-health "API not responding, restarting ${SVC}"
  systemctl restart "$SVC"
fi
