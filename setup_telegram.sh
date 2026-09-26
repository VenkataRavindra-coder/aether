#!/usr/bin/env bash
# Sets up Telegram alerts for AETHER. Run on the VM:   bash setup_telegram.sh
# The bot token is typed hidden and never printed or passed on a command line.
set -euo pipefail

APP_DIR="${AETHER_DIR:-$HOME}"
ENV_FILE="$APP_DIR/.env"
API="${TELEGRAM_API_BASE:-https://api.telegram.org}"

fail() { echo "ERROR: $*" >&2; exit 1; }
[ -f "$ENV_FILE" ] || fail "$ENV_FILE not found"

# Token goes to curl through a config on stdin so it never shows up in `ps`.
tg() {
  local method="$1"; shift
  printf 'url = "%s/bot%s/%s"\n' "$API" "$TOKEN" "$method" | curl -s -m 20 -K - "$@"
}

# Preferred: the token arrives in a private file sent straight from the Mac clipboard over SSH
# (nothing is typed or pasted into a terminal). Fallback: hidden prompt.
TOKEN_FILE="${AETHER_TOKEN_FILE:-$HOME/.aether_tg_token}"
if [ -s "$TOKEN_FILE" ]; then
  TOKEN="$(head -c 300 "$TOKEN_FILE")"
  rm -f "$TOKEN_FILE"
  echo "Using the token received over SSH (the temporary file has been deleted)."
else
  echo "Step 1: in Telegram, open @BotFather, send /newbot, follow the prompts, and copy the token."
  read -rsp "Paste the bot token (hidden): " TOKEN; echo
fi
TOKEN="${TOKEN//[[:space:]]/}"; TOKEN="${TOKEN//\"/}"; TOKEN="${TOKEN//\'/}"
# Some terminals paste twice; if the input is exactly two identical halves, keep one.
half=$(( ${#TOKEN} / 2 ))
if [ $(( ${#TOKEN} % 2 )) -eq 0 ] && [ "${TOKEN:0:$half}" = "${TOKEN:$half}" ]; then TOKEN="${TOKEN:0:$half}"; fi
if ! [[ "$TOKEN" =~ ^[0-9]{6,}:[A-Za-z0-9_-]{30,}$ ]]; then
  echo "That does not look like a full Telegram bot token." >&2
  echo "  Received ${#TOKEN} characters; a bot token is about 46: digits, a colon, then about 35 letters/digits/-/_." >&2
  case "$TOKEN" in
    *:*) echo "  It has a colon, but the parts before/after it are the wrong size." >&2 ;;
    *)   echo "  It has NO colon, so only part of the token arrived. Copy the whole token with BotFather's Copy button." >&2 ;;
  esac
  fail "Nothing was saved. Run the script again."
fi

BOT=$(tg getMe | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["result"]["username"] if d.get("ok") else "")' 2>/dev/null || true)
[ -n "$BOT" ] || fail "Telegram rejected that token."
echo "Bot found: @$BOT"

echo "Step 2: open Telegram, search for @$BOT, press Start, and send it any message (for example: hi)."
read -rp "Press Enter once you have sent it... " _

INFO=$(tg getUpdates | python3 -c '
import sys, json
d = json.load(sys.stdin)
msgs = [u["message"] for u in d.get("result", []) if u.get("message", {}).get("chat", {}).get("type") == "private"]
if msgs:
    c = msgs[-1]["chat"]
    name = (c.get("first_name", "") + " " + c.get("last_name", "")).strip() or "-"
    print(c["id"], name, ("@" + c["username"]) if c.get("username") else "")
' 2>/dev/null || true)
[ -n "$INFO" ] || fail "No message from you found. Send the bot a message, then run this script again."
CHAT_ID="${INFO%% *}"
echo "Found a message from: ${INFO#* }"
read -rp "Is that you? Alerts and commands will work ONLY for this chat. [y/N] " ANSWER
[[ "$ANSWER" =~ ^[Yy]$ ]] || fail "Aborted; nothing was saved."

echo
echo "Step 3 (recommended): an outside watcher, so you are also alerted if the whole VM is off."
echo "  Create a free check at healthchecks.io (Period: 1 minute, Grace: 5 minutes), add Telegram as"
echo "  its notification channel, and copy its ping URL."
read -rsp "Paste the ping URL, or just press Enter to skip (hidden): " HC; echo

TMP="$(mktemp)"
grep -vE '^(TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID|HEALTHCHECK_PING_URL)=' "$ENV_FILE" > "$TMP" || true
printf 'TELEGRAM_BOT_TOKEN=%s\nTELEGRAM_CHAT_ID=%s\n' "$TOKEN" "$CHAT_ID" >> "$TMP"
[ -n "$HC" ] && printf 'HEALTHCHECK_PING_URL=%s\n' "$HC" >> "$TMP"
cat "$TMP" > "$ENV_FILE"; rm -f "$TMP"
chmod 600 "$ENV_FILE"
echo "Saved to $ENV_FILE (permissions 600)."

tg sendMessage -d chat_id="$CHAT_ID" --data-urlencode "text=[AETHER] Telegram alerts are connected. Restarting the service now; send /status afterwards." >/dev/null \
  && echo "Test message sent." || echo "WARNING: could not send the test message."

if [ "${AETHER_SKIP_RESTART:-0}" != "1" ]; then
  sudo systemctl restart aether
  echo "AETHER restarted. You should receive a startup message within a few seconds."
fi
