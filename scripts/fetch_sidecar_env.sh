#!/usr/bin/env bash
# Fetch the 5 secrets from the Pi and write ~/Marked_Path/.env.sidecar in one shot.
#
# Run this ON THE SPARE (ThinkPad) as the workload user
# (e.g. remote_lakshit_gupta) AFTER:
#   - The repo is cloned somewhere (this script doesn't clone).
#   - You can ssh dietpi@<pi> without password prompts (pubkey auth set up).
#
# Replaces the heredoc-paste step that keeps tripping on auto-indent.
#
# Usage:
#   ./scripts/fetch_sidecar_env.sh
#
# Optional env:
#   PI_USER=dietpi          # SSH user on Pi
#   PI_HOST=192.168.1.240   # Pi LAN IP
#   PI_REPO=/home/dietpi/coding/Cartograph
#   OUT=.env.sidecar        # output path (defaults to current dir)
set -euo pipefail

PI_USER="${PI_USER:-dietpi}"
PI_HOST="${PI_HOST:-192.168.1.240}"
PI_REPO="${PI_REPO:-/home/dietpi/coding/Cartograph}"
OUT="${OUT:-.env.sidecar}"

echo "==> fetching 5 SOPS-decrypted values from ${PI_USER}@${PI_HOST}:${PI_REPO}"

# Run the SOPS extracts on the Pi via ssh + bash -s. The script body is sent
# as quoted-stdin so the local shell does NOT expand $VAR — Pi side does.
# Output (the rendered .env.sidecar body) streams back over stdout and gets
# redirected into the local file in one shot.
ssh "${PI_USER}@${PI_HOST}" 'bash -s' > "$OUT" <<REMOTE_SCRIPT
set -euo pipefail
cd "${PI_REPO}"
PU=\$(sops -d --extract '["postgres_user"]' secrets.yaml)
PP=\$(sops -d --extract '["postgres_password"]' secrets.yaml)
PD=\$(sops -d --extract '["postgres_db"]' secrets.yaml)
RP=\$(sops -d --extract '["redis_password"]' secrets.yaml)
LK=\$(sops -d --extract '["libsodium_master_key_hex"]' secrets.yaml)
cat <<ENV_BODY
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
POSTGRES_USER=\$PU
POSTGRES_PASSWORD=\$PP
POSTGRES_DB=\$PD

REDIS_HOST=127.0.0.1
REDIS_PORT=6379
REDIS_PASSWORD=\$RP

LIBSODIUM_MASTER_KEY_HEX=\$LK

TZ=Asia/Kolkata
PYTHONUNBUFFERED=1
ENV_BODY
REMOTE_SCRIPT

chmod 600 "$OUT"

echo "==> wrote $(wc -l < "$OUT") lines to $OUT (chmod 600)"
echo "==> sanity check (first 5 lines):"
head -5 "$OUT"

# Final check: every required value populated (not empty after the =).
missing=0
for key in POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB REDIS_PASSWORD LIBSODIUM_MASTER_KEY_HEX; do
  value=$(grep "^${key}=" "$OUT" | head -1 | cut -d= -f2-)
  if [[ -z "$value" ]]; then
    echo "[FAIL] $key is empty in $OUT"
    missing=1
  fi
done
if [[ "$missing" == "1" ]]; then
  echo "==> at least one value missing — SOPS decrypt on Pi probably failed."
  echo "    Try: ssh ${PI_USER}@${PI_HOST} 'cd ${PI_REPO} && sops -d --extract \"[\\\"postgres_user\\\"]\" secrets.yaml'"
  exit 1
fi
echo "==> all 5 values populated. .env.sidecar ready."
