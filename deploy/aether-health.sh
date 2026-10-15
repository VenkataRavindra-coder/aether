#!/usr/bin/env bash
# Restarts the engine only if its local API gives NO HTTP response at all (hung process,
# deadlock, port closed). Any HTTP status counts as alive: every path except /login and the
# webhook is behind the dashboard login, so an unauthenticated probe correctly gets 401/303.
# (2026-10-15: the first version required a 2xx from /openapi.json, got 401, and restarted the
# engine every ~90 s.)
# Skips the check for 90 s after a (re)start so slow startups are not killed.
# Service name: AETHER_SERVICE from .env (ratelark.service on Hetzner), default aether.service.
SVC="${AETHER_SERVICE:-aether.service}"
PORT="${AETHER_PORT:-8000}"
started=$(systemctl show "$SVC" -p ActiveEnterTimestampMonotonic --value)
now=$(cut -d. -f1 /proc/uptime)
if [ -n "$started" ] && [ "$started" != "0" ] && [ $((now - started / 1000000)) -lt 90 ]; then exit 0; fi
code=$(curl -s -m 15 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/login" || true)
if [ -z "$code" ] || [ "$code" = "000" ]; then
  logger -t aether-health "API gave no HTTP response, restarting ${SVC}"
  systemctl restart "$SVC"
fi
