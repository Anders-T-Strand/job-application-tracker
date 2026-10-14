#!/bin/bash
# Nightly SQLite backup to Azure Blob Storage.
# Retention without pruning: 7 rolling weekday blobs + 12 rolling monthly blobs.
#
# Requires on the host (repo root); runs under Git Bash on Windows too:
#   .backup_sas_url   - container "Blob SAS URL" with Create + Write permissions (chmod 600)
#   .backup_ping_url  - optional healthchecks.io URL pinged after success
set -euo pipefail
export MSYS_NO_PATHCONV=1  # Git Bash: don't rewrite container paths like /data into C:/...
cd "$(dirname "$0")/.."

CONTAINER=job-application-tracker-job-tracker-1
SAS_FILE=.backup_sas_url
STAGING=data/backup-staging.db

[ -f "$SAS_FILE" ] || { echo "missing $SAS_FILE — see README 'Backups'"; exit 1; }
SAS_URL=$(cat "$SAS_FILE")
BASE="${SAS_URL%%\?*}"
QUERY="${SAS_URL#*\?}"

# Consistent snapshot via SQLite's backup API (safe against a live WAL database).
docker exec "$CONTAINER" python -c "
import sqlite3
src = sqlite3.connect('/data/tracker.db')
dst = sqlite3.connect('/data/backup-staging.db')
src.backup(dst)
dst.close()
src.close()
"

upload() {
  curl -fsS --max-time 120 -X PUT \
    -H "x-ms-blob-type: BlockBlob" \
    --data-binary @"$STAGING" \
    -o /dev/null \
    "$BASE/$1?$QUERY"
  echo "$(date -u +%FT%TZ) uploaded $1 ($(du -h "$STAGING" | cut -f1))"
}

upload "tracker-$(date +%a).db"    # Mon..Sun, overwritten weekly
upload "tracker-m$(date +%m).db"   # 01..12, overwritten yearly

if [ -f .backup_ping_url ]; then
  curl -fsS -o /dev/null "$(cat .backup_ping_url)" || true
fi
