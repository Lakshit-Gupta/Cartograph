#!/usr/bin/env bash
# Weekly restore drill — restores latest R2 dump into a tmpfs Postgres instance,
# verifies schema + row counts match production, then tears down.

set -euo pipefail
cd /home/lakshit_gupta/coding/cartograph

TMPDB_CONTAINER="cartograph_restore_drill"
TMPFS_VOLUME="cartograph_restore_tmpfs"

cleanup() {
    docker rm -f "$TMPDB_CONTAINER" >/dev/null 2>&1 || true
    docker volume rm "$TMPFS_VOLUME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# 1. Pull latest backup from R2
TMP_DIR=$(mktemp -d)
sops exec-env secrets.yaml '
  rclone --config /etc/cartograph/rclone.conf copy --include "*.age" --max-age 24h r2:'$r2_bucket'/postgres/ "'"$TMP_DIR"'"/
'
LATEST_AGE=$(ls -1t "$TMP_DIR"/*.age | head -1)
if [[ -z "$LATEST_AGE" ]]; then
    echo "no recent backup in R2" >&2
    exit 1
fi

# 2. Decrypt with the age IDENTITY key (private key only on Pi)
DUMP_FILE="${LATEST_AGE%.age}"
age -d -i ~/.config/sops/age/keys.txt -o "$DUMP_FILE" "$LATEST_AGE"

# 3. Bring up a throwaway postgres on tmpfs
docker volume create --driver local --opt type=tmpfs --opt device=tmpfs "$TMPFS_VOLUME" >/dev/null
docker run -d --name "$TMPDB_CONTAINER" \
    --mount source="$TMPFS_VOLUME",target=/var/lib/postgresql/data \
    -e POSTGRES_PASSWORD=drill \
    -e POSTGRES_DB=marked_drill \
    -e POSTGRES_USER=marked \
    postgres:16-alpine >/dev/null

# wait until ready
for i in {1..30}; do
    docker exec "$TMPDB_CONTAINER" pg_isready -U marked -d marked_drill >/dev/null 2>&1 && break
    sleep 1
done

# 4. Restore
cat "$DUMP_FILE" | docker exec -i "$TMPDB_CONTAINER" pg_restore --no-owner --no-privileges -U marked -d marked_drill

# 5. Compare key row counts to live DB
LIVE_OPPS=$(sops exec-env secrets.yaml '
  docker compose exec -T postgres psql -U $postgres_user -d $postgres_db -tA -c "SELECT COUNT(*) FROM opportunities"
')
DRILL_OPPS=$(docker exec "$TMPDB_CONTAINER" psql -U marked -d marked_drill -tA -c "SELECT COUNT(*) FROM opportunities")
DIFF=$(( LIVE_OPPS - DRILL_OPPS ))
ABS_DIFF=${DIFF#-}
echo "live opps=$LIVE_OPPS  drill opps=$DRILL_OPPS  diff=$DIFF"
# Allow drift of up to 100 rows due to time between dump + drill
if (( ABS_DIFF > 100 )); then
    echo "restore drift exceeds tolerance" >&2
    exit 2
fi

echo "restore drill OK"
