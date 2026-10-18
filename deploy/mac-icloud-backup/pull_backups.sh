#!/bin/bash
# Runs on YOUR MAC. Pulls AETHER backups from the server into iCloud Drive, checks them, then tells the server.
# Needs: ~/.ssh/aether_backup_key (no passphrase; restricted on the server to read-only backups + receipts).
set -uo pipefail
SERVER=${AETHER_SERVER:-ratelark@2.28.67.174}
KEY=${AETHER_BACKUP_KEY:-$HOME/.ssh/aether_backup_key}
DEST=${AETHER_ICLOUD_DIR:-$HOME/Library/Mobile Documents/com~apple~CloudDocs/AETHER-backups}
LOG=${AETHER_LOG:-$HOME/Library/Logs/aether-icloud-backup.log}
SSH="ssh -i $KEY -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=20"
say() { echo "$(date '+%F %T') $*" >> "$LOG"; }

mkdir -p "$DEST" || { say "FAIL cannot create $DEST (macOS may need Full Disk Access for /bin/bash)"; exit 1; }
if ! rsync -a --partial -e "$SSH" "$SERVER:/" "$DEST/" 2>>"$LOG"; then say "FAIL rsync from server"; exit 1; fi

newest=$(ls -1 "$DEST"/aether-*.tar.gz 2>/dev/null | sort | tail -n 1)
[ -n "$newest" ] || { say "FAIL no archives in $DEST"; exit 1; }
gzip -t "$newest" 2>>"$LOG" || { say "FAIL $(basename "$newest") is corrupt"; exit 1; }
name=$(basename "$newest")
# Only now tell the server: it records a receipt for this archive, which the dashboard then shows.
if $SSH "$SERVER" "receipt $name" >>"$LOG" 2>&1; then say "OK $name pulled, verified, receipt sent"; else say "FAIL receipt for $name was not accepted"; exit 1; fi
# Keep the newest 90 copies in iCloud.
ls -1 "$DEST"/aether-*.tar.gz | sort | head -n -90 | while read -r f; do rm -- "$f"; done
