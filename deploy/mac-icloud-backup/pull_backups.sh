#!/bin/bash
# Runs on YOUR MAC. Pulls AETHER backups from the server into iCloud Drive, checks them, then tells the server.
# Needs: ~/.ssh/aether_backup_key (no passphrase; restricted on the server to list/get backups + receipts).
set -uo pipefail
SERVER=${AETHER_SERVER:-ratelark@2.28.67.174}
KEY=${AETHER_BACKUP_KEY:-$HOME/.ssh/aether_backup_key}
DEST=${AETHER_ICLOUD_DIR:-$HOME/Library/Mobile Documents/com~apple~CloudDocs/AETHER-backups}
LOG=${AETHER_LOG:-$HOME/Library/Logs/aether-icloud-backup.log}
SSH=(ssh -n -i "$KEY" -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=20)
say() { echo "$(date '+%F %T') $*" >> "$LOG"; }

mkdir -p "$DEST" || { say "FAIL cannot create $DEST (macOS may need Full Disk Access for /bin/bash)"; exit 1; }
listing=$("${SSH[@]}" "$SERVER" list 2>>"$LOG") || { say "FAIL cannot list backups on the server"; exit 1; }
[ -n "$listing" ] || { say "FAIL the server has no archives"; exit 1; }

newest=""
while read -r name size; do
  [ -n "$name" ] || continue
  newest=$name
  if [ -f "$DEST/$name" ] && [ "$(stat -f %z "$DEST/$name")" = "$size" ]; then continue; fi   # already here, same size
  tmp="$DEST/.$name.part"
  if ! "${SSH[@]}" "$SERVER" "get $name" > "$tmp" 2>>"$LOG"; then rm -f "$tmp"; say "FAIL download of $name"; exit 1; fi
  if [ "$(stat -f %z "$tmp")" != "$size" ] || ! gzip -t "$tmp" 2>>"$LOG"; then rm -f "$tmp"; say "FAIL $name is incomplete or corrupt"; exit 1; fi
  mv "$tmp" "$DEST/$name"
done <<< "$(printf '%s\n' "$listing" | sort)"

[ -f "$DEST/$newest" ] || { say "FAIL newest archive missing locally"; exit 1; }
# Only now tell the server: it records a receipt for this archive, which the dashboard then shows.
if "${SSH[@]}" "$SERVER" "receipt $newest" >>"$LOG" 2>&1; then say "OK $newest pulled, verified, receipt sent"; else say "FAIL receipt for $newest was not accepted"; exit 1; fi
# Keep the newest 90 copies in iCloud.
ls -1 "$DEST"/aether-*.tar.gz | sort | awk '{a[NR]=$0} END{for(i=1;i<=NR-90;i++) print a[i]}' | while read -r f; do rm -- "$f"; done   # BSD head has no negative counts
