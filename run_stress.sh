#!/usr/bin/env bash
# Runs the stress test on the dedicated stress account and records the result for the daily runner (A5).
# Started by aether-stress.timer on weekdays at 16:00 UTC (inside US market hours all year).
cd /home/azureuser
rc=0
/home/azureuser/venv/bin/python3 stress_test.py || rc=$?
printf '{"rc": %d, "utc": "%s"}\n' "$rc" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > /home/azureuser/.stress_last_run.json
exit "$rc"
