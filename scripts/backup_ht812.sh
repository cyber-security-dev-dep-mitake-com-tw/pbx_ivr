#!/usr/bin/env bash
# Trigger HT812V2 config backup via the ht812_api service.
# Saves a timestamped XML file to ./backups/ and prints the filename.

set -euo pipefail

API_BASE="${HT812_API_URL:-http://localhost:8000}"
BACKUP_DIR="${BACKUP_DIR:-$(cd "$(dirname "$0")/.." && pwd)/backups}"

echo "Triggering HT812 config backup via $API_BASE/ht812/config ..."

RESPONSE=$(curl -sf -o /tmp/ht812_backup_tmp.xml \
    -w "%{http_code}" \
    -H "Accept: application/xml" \
    "${API_BASE}/ht812/config")

if [ "$RESPONSE" != "200" ]; then
    echo "ERROR: API returned HTTP $RESPONSE" >&2
    exit 1
fi

# The API already saves the file to the container's /backups volume.
# This script also keeps a local copy timestamped to the second.
TIMESTAMP=$(date -u +"%Y%m%dT%H%M%SZ")
LOCAL_FILE="${BACKUP_DIR}/ht812_config_${TIMESTAMP}.xml"
mkdir -p "$BACKUP_DIR"
cp /tmp/ht812_backup_tmp.xml "$LOCAL_FILE"
rm -f /tmp/ht812_backup_tmp.xml

echo "Saved: $LOCAL_FILE"
echo "Size:  $(wc -c < "$LOCAL_FILE") bytes"
