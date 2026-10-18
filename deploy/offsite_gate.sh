#!/usr/bin/env bash
# Forced command for the Mac's backup key (authorized_keys: command="...offsite_gate.sh",restrict ssh-ed25519 ...).
# The key can do exactly two things and nothing else (no shell, no other files):
#   1. "list" and "get <archive name>": read-only access to the archives (the Mac pulls them; no rsync needed)
#   2. "receipt <archive name>"                      (the Mac confirms it holds a verified copy)
# A receipt is accepted only for a real archive that exists here, so the dashboard cannot be told about a
# copy that does not exist.
set -euo pipefail
BACKUP_DIR=${AETHER_BACKUP_DIR:-/home/ratelark/ratelark/backups}
RECEIPT=${AETHER_RECEIPT:-/home/ratelark/ratelark/.offsite_receipt}
cmd=${SSH_ORIGINAL_COMMAND:-}
case "$cmd" in
  "receipt "*)
    name=${cmd#receipt }
    if [[ "$name" =~ ^aether-[0-9]{8}T[0-9]{6}Z\.tar\.gz$ ]] && [ -f "$BACKUP_DIR/$name" ]; then
      printf '%s %s icloud\n' "$name" "$(date -u +%s)" > "$RECEIPT.tmp" && mv "$RECEIPT.tmp" "$RECEIPT"
      echo ok
    else
      echo "refused: not an archive on this server" >&2; exit 1
    fi ;;
  "list")
    for f in "$BACKUP_DIR"/aether-*.tar.gz; do [ -f "$f" ] && printf '%s %s\n' "$(basename "$f")" "$(stat -c %s "$f")"; done; true ;;
  "get "*)
    name=${cmd#get }
    if [[ "$name" =~ ^aether-[0-9]{8}T[0-9]{6}Z\.tar\.gz$ ]] && [ -f "$BACKUP_DIR/$name" ]; then cat "$BACKUP_DIR/$name"
    else echo "refused: not an archive on this server" >&2; exit 1; fi ;;
  *)
    echo "refused" >&2; exit 1 ;;
esac
