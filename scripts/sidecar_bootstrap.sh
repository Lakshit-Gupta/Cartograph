#!/usr/bin/env bash
# Sidecar bootstrap — Pop OS 24.04 desktop (x86_64), spare machine.
#
# Run as `remote_lakshit_gupta` (NOT root) on the spare. Idempotent:
# every step checks before mutating. Pi-side prep (carto-tunnel user,
# sshd_config) is OUT of scope here — see docs/runbooks/sidecar_setup.md
# sections 2.1-2.4.
#
# Usage:
#   ./scripts/sidecar_bootstrap.sh
#
# Optional env:
#   PI_HOST=192.168.1.240      # Pi LAN IP (same WiFi as the spare)
#   REPO_DIR=$HOME/Marked_Path # where to clone if missing
#   SKIP_DOCKER=0              # set to 1 if docker already installed
#   SKIP_SSHKEY=0              # set to 1 to reuse existing carto_tunnel key
set -euo pipefail

PI_HOST="${PI_HOST:-192.168.1.240}"
REPO_DIR="${REPO_DIR:-$HOME/Marked_Path}"
SKIP_DOCKER="${SKIP_DOCKER:-0}"
SKIP_SSHKEY="${SKIP_SSHKEY:-0}"
SSH_KEY="$HOME/.ssh/carto_tunnel_ed25519"

log() { printf '\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[warn] %s\033[0m\n' "$*" >&2; }

if [[ "$(id -un)" == "root" ]]; then
    warn "running as root — bootstrap script expects an unprivileged user (e.g. remote_lakshit_gupta)."
    exit 1
fi

log "spare host: $(hostname) ($(uname -srm))"
log "user: $(id -un)  pi target: $PI_HOST  repo: $REPO_DIR"

# 1) base packages -----------------------------------------------------------
log "step 1 — base packages (apt)"
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    ca-certificates curl git openssh-client autossh \
    redis-tools postgresql-client netcat-openbsd chrony jq

# 2) docker engine (skip if already present) --------------------------------
if [[ "$SKIP_DOCKER" != "1" ]] && ! command -v docker >/dev/null 2>&1; then
    log "step 2 — install docker engine via get.docker.com"
    curl -fsSL https://get.docker.com | sudo sh
    sudo apt-get install -y docker-compose-plugin
    sudo usermod -aG docker "$(id -un)"
    warn "you must log out + log back in (or 'newgrp docker') for the docker group to take effect."
else
    log "step 2 — docker already installed; skipping"
fi

# 3) host hardening ---------------------------------------------------------
log "step 3 — disable suspend / hibernate (desktop is always-on for the sidecar)"
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target || true

log "step 3b — enable chrony for clock sync"
sudo systemctl enable --now chrony

log "step 3c — docker log rotation"
if [[ ! -f /etc/docker/daemon.json ]] || ! jq -e '."log-driver"' /etc/docker/daemon.json >/dev/null 2>&1; then
    sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "50m", "max-file": "3" }
}
EOF
    sudo systemctl restart docker
fi

# 4) ssh key for carto-tunnel -----------------------------------------------
if [[ "$SKIP_SSHKEY" != "1" && ! -f "$SSH_KEY" ]]; then
    log "step 4 — generate ed25519 keypair for carto-tunnel"
    ssh-keygen -t ed25519 -f "$SSH_KEY" -C "carto-tunnel@$(hostname)" -N ""
fi

log "step 4b — public key follows. Paste it into /home/carto-tunnel/.ssh/authorized_keys on the Pi"
log "          with the restriction prefix from docs/runbooks/sidecar_setup.md §3.1, then press Enter."
cat "${SSH_KEY}.pub"
read -r -p "paste done; press Enter to smoke-test the tunnel >> "

# 5) tunnel smoke test ------------------------------------------------------
log "step 5 — smoke test SSH tunnel (Ctrl-C after PONG to continue)"
ssh -i "$SSH_KEY" -N \
    -L 127.0.0.1:6379:127.0.0.1:6379 \
    -L 127.0.0.1:5432:127.0.0.1:5432 \
    -o ExitOnForwardFailure=yes \
    -o StrictHostKeyChecking=accept-new \
    "carto-tunnel@${PI_HOST}" &
TUNNEL_PID=$!
sleep 4
if nc -zv 127.0.0.1 6379 && nc -zv 127.0.0.1 5432; then
    log "tunnel OK (both ports reachable on 127.0.0.1)"
else
    warn "tunnel failed — kill the SSH process, fix authorized_keys, rerun bootstrap."
    kill "$TUNNEL_PID" >/dev/null 2>&1 || true
    exit 1
fi
kill "$TUNNEL_PID" >/dev/null 2>&1 || true

# 6) repo + .env.sidecar ----------------------------------------------------
if [[ ! -d "$REPO_DIR/.git" ]]; then
    log "step 6 — clone repo to $REPO_DIR"
    git clone https://github.com/Lakshit-Gupta/Cartograph.git "$REPO_DIR"
fi
cd "$REPO_DIR"
git pull --ff-only || warn "git pull failed (uncommitted local edits?); continuing with current checkout."

if [[ ! -f "$REPO_DIR/.env.sidecar" ]]; then
    log "step 6b — copy .env.sidecar.example -> .env.sidecar; fill values then re-run"
    cp .env.sidecar.example .env.sidecar
    chmod 600 .env.sidecar
    warn "EDIT $REPO_DIR/.env.sidecar with the SOPS-extracted values from the Pi"
    warn "(postgres_user, postgres_password, postgres_db, redis_password, libsodium_master_key_hex)"
    warn "then re-run this script. Halting now."
    exit 0
fi

# 7) autossh systemd unit ---------------------------------------------------
log "step 7 — install autossh systemd unit"
sudo tee /etc/systemd/system/carto-tunnel.service >/dev/null <<EOF
[Unit]
Description=Persistent SSH tunnel to Pi (Redis + Postgres forwards)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(id -un)
Group=$(id -gn)

Environment=AUTOSSH_GATETIME=0
Environment=AUTOSSH_PORT=0

ExecStart=/usr/bin/autossh -N \\
  -o ExitOnForwardFailure=yes \\
  -o ServerAliveInterval=30 \\
  -o ServerAliveCountMax=3 \\
  -o StrictHostKeyChecking=yes \\
  -o UserKnownHostsFile=${HOME}/.ssh/known_hosts \\
  -o IdentitiesOnly=yes \\
  -i ${SSH_KEY} \\
  -L 127.0.0.1:6379:127.0.0.1:6379 \\
  -L 127.0.0.1:5432:127.0.0.1:5432 \\
  carto-tunnel@${PI_HOST}

Restart=always
RestartSec=10s

NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=${HOME}/.ssh
PrivateTmp=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now carto-tunnel.service
sleep 3
sudo systemctl --no-pager status carto-tunnel.service | head -15

# 8) build images ----------------------------------------------------------
log "step 8 — build camoufox + apply-browser images (native x86_64; no QEMU)"
docker build -f docker/camoufox.Dockerfile -t marked_path-camoufox-worker:latest .
docker build -f docker/apply_browser.Dockerfile -t marked_path-apply-browser:latest .

# 9) bring up sidecar -------------------------------------------------------
log "step 9 — docker compose up -d (compose.sidecar.yaml)"
docker compose -f compose.sidecar.yaml up -d

log "step 10 — tailing apply-browser-worker logs (Ctrl-C to exit)"
docker compose -f compose.sidecar.yaml logs --tail=40 -f apply-browser-worker
