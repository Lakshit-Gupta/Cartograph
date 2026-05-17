#!/usr/bin/env bash
# Idempotent installer for Marked_Path cron jobs.
# Adds three entries to the invoking user's crontab:
#   1. Nightly Postgres backup -> age encrypt -> rclone -> R2 (03:30 daily)
#   2. Weekly restore drill into tmpfs (04:00 Sundays)
#   3. Nightly pg_amcheck consistency check (05:00 daily)
#
# All managed lines carry the trailing marker `# marked_path_cron` so reruns
# strip the old block and reinstall cleanly.
#
# RUN ON THE PI ONLY. Guarded by MARKED_PATH_PI_CONFIRM=1.
# After writing this file, mark it executable on the Pi:
#   chmod +x scripts/install_cron.sh

set -euo pipefail

if [[ -z "${MARKED_PATH_PI_CONFIRM:-}" ]]; then
    echo "Run on the Pi only" >&2
    echo "  Set MARKED_PATH_PI_CONFIRM=1 in the environment before invoking." >&2
    exit 1
fi

# Ensure the log directory exists; cron entries redirect into it.
mkdir -p /var/lib/agent/logs

TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

# Strip any previously-managed marked_path_cron lines, preserving everything else.
crontab -l 2>/dev/null | grep -v "marked_path_cron" > "$TMP" || true

cat >> "$TMP" <<'EOF'
# marked_path_cron --- managed by scripts/install_cron.sh (do not edit manually)
30 3 * * *   cd /home/lakshit_gupta/coding/Marked_Path && bash scripts/backup.sh >> /var/lib/agent/logs/backup.log 2>&1  # marked_path_cron
0  4 * * 0   cd /home/lakshit_gupta/coding/Marked_Path && bash scripts/restore_drill.sh >> /var/lib/agent/logs/restore_drill.log 2>&1  # marked_path_cron
0  5 * * *   cd /home/lakshit_gupta/coding/Marked_Path && docker compose exec -T postgres pg_amcheck -U marked marked --verbose >> /var/lib/agent/logs/pg_amcheck.log 2>&1  # marked_path_cron
EOF

crontab "$TMP"

echo "Installed Marked_Path cron entries. Verify with:"
echo "  crontab -l | grep marked_path_cron"
