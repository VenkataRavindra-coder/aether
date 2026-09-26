#!/usr/bin/env bash
# Installs AETHER as a systemd service: starts on boot, restarts on crash, and a
# watchdog timer restarts it if it stops answering. Run on the VM as a normal
# user with sudo access:   bash setup_service.sh
set -euo pipefail

APP_DIR="${AETHER_DIR:-$HOME}"
RUN_USER="$(id -un)"
PY="${AETHER_PYTHON:-$(command -v python3)}"
PORT="${AETHER_PORT:-8000}"
# Loopback by default: only the local reverse proxy (HTTPS) and SSH tunnels can reach AETHER.
HOST="${AETHER_HOST:-127.0.0.1}"

fail() { echo "ERROR: $*" >&2; exit 1; }

[ -f "$APP_DIR/aether_engine.py" ]   || fail "aether_engine.py not found in $APP_DIR"
[ -f "$APP_DIR/alpaca_executor.py" ] || fail "alpaca_executor.py not found in $APP_DIR"
[ -f "$APP_DIR/strategy.py" ]        || fail "strategy.py not found in $APP_DIR"
[ -f "$APP_DIR/.env" ]               || fail ".env not found in $APP_DIR"
grep -q '^WEBHOOK_SECRET=.\+' "$APP_DIR/.env" || fail "WEBHOOK_SECRET is missing or empty in .env"
grep -q '^APCA_API_KEY_ID=.\+' "$APP_DIR/.env" || fail "APCA_API_KEY_ID is missing in .env"
grep -q '^LIVE_TRADING=' "$APP_DIR/.env" || echo "LIVE_TRADING=false" >> "$APP_DIR/.env"
"$PY" -c "import fastapi, uvicorn, alpaca, dotenv, pydantic" 2>/dev/null \
  || fail "$PY is missing required packages. Activate your venv first (source <venv>/bin/activate) and re-run."
# Absolute path, but do NOT resolve symlinks: a venv python is a symlink to the system
# interpreter, and resolving it would silently drop the venv's installed packages.
PY="$(cd "$(dirname "$PY")" && pwd)/$(basename "$PY")"

echo "App dir : $APP_DIR"
echo "User    : $RUN_USER"
echo "Python  : $PY"
echo "Port    : $PORT"
echo "Bind    : $HOST"
echo "Mode    : $(grep '^LIVE_TRADING=' "$APP_DIR/.env" | tail -1)"

chmod 600 "$APP_DIR/.env"
(cd "$APP_DIR" && "$PY" -m py_compile aether_engine.py alpaca_executor.py strategy.py) || fail "Python files do not compile."

echo "Stopping any manually started (nohup) instance..."
pkill -f "uvicorn aether_engine" 2>/dev/null || true
sleep 1

echo "Installing systemd units (sudo required)..."
sudo tee /etc/systemd/system/aether.service >/dev/null <<UNIT
[Unit]
Description=AETHER autonomous trading engine
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$PY -m uvicorn aether_engine:app --host $HOST --port $PORT --no-access-log
Restart=always
RestartSec=5
TimeoutStopSec=20
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full

[Install]
WantedBy=multi-user.target
UNIT

sudo tee /usr/local/bin/aether-health.sh >/dev/null <<HEALTH
#!/usr/bin/env bash
# Restarts AETHER if the local API stops answering (hung process, deadlock, etc).
# Skips the check for 90s after a (re)start so slow startups are not killed.
started=\$(systemctl show aether.service -p ActiveEnterTimestampMonotonic --value)
now=\$(cat /proc/uptime | cut -d. -f1)
if [ -n "\$started" ] && [ "\$started" != "0" ] && [ \$((now - started / 1000000)) -lt 90 ]; then exit 0; fi
if ! curl -sf -m 15 -o /dev/null http://127.0.0.1:$PORT/openapi.json; then
  logger -t aether-health "API not responding, restarting aether.service"
  systemctl restart aether.service
fi
HEALTH
sudo chmod 755 /usr/local/bin/aether-health.sh

sudo tee /etc/systemd/system/aether-health.service >/dev/null <<UNIT
[Unit]
Description=AETHER health check

[Service]
Type=oneshot
ExecStart=/usr/local/bin/aether-health.sh
UNIT

sudo tee /etc/systemd/system/aether-health.timer >/dev/null <<UNIT
[Unit]
Description=Run AETHER health check every minute

[Timer]
OnBootSec=3min
OnUnitActiveSec=1min
AccuracySec=10s

[Install]
WantedBy=timers.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now aether.service
sudo systemctl enable --now aether-health.timer

echo "Waiting for the API to come up..."
for i in $(seq 1 30); do
  if curl -sf -m 3 -o /dev/null "http://127.0.0.1:$PORT/openapi.json"; then ok=1; break; fi
  sleep 2
done

echo
systemctl --no-pager --lines=0 status aether.service | head -5 || true
systemctl is-enabled aether.service aether-health.timer
echo
if [ "${ok:-0}" = "1" ]; then
  echo "SUCCESS: AETHER is running as a service and will survive crashes and reboots."
else
  echo "WARNING: API did not answer within 60s. Check: sudo journalctl -u aether -n 50 --no-pager"
  exit 1
fi
echo "Logs:     sudo journalctl -u aether -f"
echo "Restart:  sudo systemctl restart aether"
echo "Stop:     sudo systemctl stop aether"
