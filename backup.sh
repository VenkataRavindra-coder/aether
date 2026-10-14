#!/usr/bin/env bash
# AETHER nightly backup: engine state + decision log + research trials.
# Local: ~/backups/aether-<UTC>.tar.gz, last 14 kept.
# Offsite: Azure Blob via BACKUP_SAS_URL in .env (container SAS, Create permission only).
# Never includes .env or keys: secrets live in the password manager.
set -euo pipefail
trap '/home/ratelark/ratelark/.venv/bin/python3 /home/ratelark/ratelark/backup_alert.py "backup failed at line $LINENO"' ERR

HOME_DIR=/home/ratelark/ratelark
OUT="$HOME_DIR/backups"
TS=$(date -u +%Y%m%dT%H%M%SZ)
WORK="$OUT/$TS"
cd "$HOME_DIR"
mkdir -p "$WORK"
chmod 700 "$OUT"

# 1. Consistent copies: sqlite .backup is safe while the engine is writing (WAL).
sqlite3 aether_decisions.sqlite ".backup '$WORK/aether_decisions.sqlite'"
sqlite3 aether-research/research.sqlite ".backup '$WORK/research.sqlite'"
cp aether_state.json "$WORK/"
[ -f .aether_risk_state.json ] && cp .aether_risk_state.json "$WORK/"
[ -f .aether_canary_risk_state.json ] && cp .aether_canary_risk_state.json "$WORK/"
[ -f live_risk_ledger.json ] && cp live_risk_ledger.json "$WORK/"
[ -d tests/evidence ] && cp -r tests/evidence "$WORK/"
git rev-parse HEAD > "$WORK/git_commit.txt"

# 2. Verify before trusting it.
for db in "$WORK"/*.sqlite; do
  [ "$(sqlite3 "$db" 'pragma integrity_check;')" = "ok" ] || { echo "integrity failed: $db"; false; }
done
/home/ratelark/ratelark/.venv/bin/python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$WORK/aether_state.json"

# 3. Pack.
ARCHIVE="$OUT/aether-$TS.tar.gz"
tar -czf "$ARCHIVE" -C "$OUT" "$TS"
rm -rf "$WORK"
chmod 600 "$ARCHIVE"

# 4. Offsite (skipped with a warning until BACKUP_SAS_URL is set).
SAS=$(grep -E '^BACKUP_SAS_URL=' .env | cut -d= -f2- | tr -d '"' || true)
if [ -n "$SAS" ]; then
  URL="${SAS%%\?*}/$(basename "$ARCHIVE")?${SAS#*\?}"
  curl -fsS --retry 3 -X PUT -H "x-ms-blob-type: BlockBlob" --data-binary @"$ARCHIVE" "$URL" -o /dev/null
  echo "uploaded $(basename "$ARCHIVE") offsite"
else
  echo "WARNING: BACKUP_SAS_URL not set, local backup only"
fi

# 5. Keep the newest 14 local archives.
ls -1t "$OUT"/aether-*.tar.gz | tail -n +15 | xargs -r rm --
echo "backup ok: $(basename "$ARCHIVE") $(du -h "$ARCHIVE" | cut -f1)"
