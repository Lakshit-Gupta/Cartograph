#!/usr/bin/env bash
# Marked_Path first-time bootstrap. RUN ON THE PI ONLY — never on the dev laptop.
# Handles: swap, fonts, log dir, WAL archive dir, fsck flag, host port reservations.

set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
    echo "Marked_Path bootstrap targets Linux only." >&2
    exit 1
fi

if [[ "$EUID" -ne 0 ]]; then
    echo "Run as root: sudo bash scripts/bootstrap.sh" >&2
    exit 1
fi

if [[ "$(hostname)" == *"dev"* || -z "${MARKED_PATH_PI_CONFIRM:-}" ]]; then
    echo "Refusing to run unless MARKED_PATH_PI_CONFIRM=1 is set in env." >&2
    echo "Set MARKED_PATH_PI_CONFIRM=1 only on the Raspberry Pi." >&2
    exit 1
fi

echo "[1/6] 4GB swap (skips if already mounted)"
if ! swapon --show | grep -q /swapfile; then
    dd if=/dev/zero of=/swapfile bs=1M count=4096
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    if ! grep -q '^/swapfile' /etc/fstab; then
        echo '/swapfile none swap sw 0 0' >> /etc/fstab
    fi
fi

echo "[2/6] CF fingerprint defense fonts"
apt-get update -qq
apt-get install -y --no-install-recommends \
    fonts-liberation fonts-noto fonts-noto-cjk fonts-dejavu fonts-roboto \
    ttf-mscorefonts-installer || true

echo "[3/6] Agent log + WAL archive dirs"
mkdir -p /var/lib/agent/logs
chmod 755 /var/lib/agent/logs
mkdir -p /mnt/storage/wal_archive /mnt/storage/agent-backups /mnt/storage/obsidian_vault
chown -R 999:999 /mnt/storage/wal_archive    # postgres container uid is 999

echo "[4/6] fsck on every boot (no UPS = power-fail risk)"
ROOT_DEV=$(findmnt -no SOURCE / 2>/dev/null || true)
if [[ -n "$ROOT_DEV" ]]; then
    tune2fs -c 1 "$ROOT_DEV" || true
fi

echo "[5/6] Reserved ports check (Docker-internal only)"
for port in 5432 6379 9090 8191; do
    if ss -lntp | grep -q ":$port "; then
        echo "  warn: port $port in use on host — Docker will bind internally only, OK"
    fi
done

echo "[6/6] Pre-flight verify"
docker --version
docker compose version

echo "[7/7] Install Marked_Path cron entries (backup, restore drill, pg_amcheck)"
bash scripts/install_cron.sh

echo
echo "Bootstrap complete. Next steps:"
echo "  1) sops --encrypt --age <pubkey> --in-place secrets.yaml"
echo "  2) make up"
echo "  3) make migrate && make seed"
echo "  4) Verify cron: crontab -l | grep marked_path_cron  (expect 3 entries)"
