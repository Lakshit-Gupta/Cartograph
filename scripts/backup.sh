#!/usr/bin/env bash
# Nightly backup: pg_dump → age encrypt → rclone copy → R2.
# Cron via host crontab: 30 3 * * * /home/lakshit_gupta/coding/Marked_Path/scripts/backup.sh

set -euo pipefail
cd /home/lakshit_gupta/coding/Marked_Path

DATE=$(date -u +%Y%m%dT%H%M%SZ)
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

# Run inside the tools profile container so we don't need pg_dump on the host
DUMP_FILE="$TMP_DIR/marked_path_$DATE.sql.gz"
sops exec-env secrets.yaml '
  docker compose run --rm tools \
    bash -c "pg_dump -h postgres -U $postgres_user -d $postgres_db --no-owner --no-privileges --clean --if-exists -Fc"
' > "$DUMP_FILE"

# Encrypt with the age recipient pubkey listed in secrets/age_recipients.txt
AGE_RECIPIENTS_FILE="${AGE_RECIPIENTS_FILE:-/etc/marked_path/age_recipients.txt}"
if [[ ! -f "$AGE_RECIPIENTS_FILE" ]]; then
    echo "missing age recipients file at $AGE_RECIPIENTS_FILE" >&2
    exit 1
fi
ENC_FILE="${DUMP_FILE}.age"
age -R "$AGE_RECIPIENTS_FILE" -o "$ENC_FILE" "$DUMP_FILE"

# Copy to R2 via rclone (rclone config provided externally; remote name "r2")
sops exec-env secrets.yaml '
  rclone --config /etc/marked_path/rclone.conf copy "'"$ENC_FILE"'" r2:'$r2_bucket'/postgres/
'

echo "backup uploaded: $(basename "$ENC_FILE")"
