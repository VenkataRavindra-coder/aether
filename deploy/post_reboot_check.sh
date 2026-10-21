#!/usr/bin/env bash
# Read-only check to run after a reboot (or any time): did everything come back by itself?
# Changes nothing. Run:  bash ~/ratelark/deploy/post_reboot_check.sh
# Reboot test: `sudo reboot`, wait 2 minutes, ssh back in, run this. Every line should say OK.
SVC="${AETHER_SERVICE:-ratelark.service}"
PORT="${AETHER_PORT:-8000}"
fail=0
ok()   { printf '  OK    %s\n' "$1"; }
bad()  { printf '  FAIL  %s\n' "$1"; fail=1; }
chk()  { if eval "$2" >/dev/null 2>&1; then ok "$1"; else bad "$1"; fi; }

echo "Server up for: $(uptime -p)"
echo "Services"
for u in "$SVC" cloudflared.service; do
  chk "$u enabled at boot"  "systemctl is-enabled $u"
  chk "$u running"          "systemctl is-active $u"
  chk "$u restarts itself"  "[ \"\$(systemctl show $u -p Restart --value)\" = always ]"
done
echo "Timers"
for t in health watchdog backup daily stress riskmark canary-run canary-close; do
  chk "aether-$t.timer active" "systemctl is-active aether-$t.timer"
done
echo "Dashboard"
code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/login")
if [ -n "$code" ] && [ "$code" != "000" ]; then ok "local dashboard answers (HTTP $code)"; else bad "local dashboard gives no answer"; fi
envf="$(dirname "$0")/../.env"
if [ -f "$envf" ] && grep -Eq '^(HEALTHCHECK_URL|HEALTHCHECK_PING_URL)=.+' "$envf"; then ok "outside monitor URL is set (HEALTHCHECK_URL)"; else bad "HEALTHCHECK_URL not set in .env: nothing outside the server will notice if it dies"; fi
[ "$fail" = 0 ] && echo "ALL OK" || echo "SOMETHING NEEDS FIXING (see FAIL lines)"
exit "$fail"
