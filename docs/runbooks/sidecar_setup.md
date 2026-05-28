# Browser-sidecar setup (camoufox-worker on a spare machine, SSH-tunneled)

Adds a second physical host running additional `camoufox-worker` replicas
that connect to the Raspberry Pi's Redis + Postgres over an SSH tunnel.
Different egress IP from the Pi → Cloudflare cannot link the spare's
browser fingerprints to the Pi's burned IP. The spare also offloads the
Pi's RAM (camoufox×3 currently ~3 GB of 8 GB).

```
[ spare 192.168.1.x ]  -- ssh tunnel -->  [ Pi 192.168.1.240 ]
  autossh systemd unit:                     sshd: user carto-tunnel
   -L 127.0.0.1:6379 ─►                       (nologin, port-forward only,
   -L 127.0.0.1:5432 ─►                        permitopen restricted)
                                                       │
  camoufox-worker container                            ▼
  (network_mode: host,                       127.0.0.1:6379 redis (loopback bind)
   REDIS_HOST=127.0.0.1,                     127.0.0.1:5432 postgres (loopback bind)
   POSTGRES_HOST=127.0.0.1)
```

Routing model: spare workers join the existing `g:crawlers` consumer
group on `stream:fetch`. Redis Streams load-share natively — no code
change required. Each consumer name is `{hostname}-{pid}`
(`src/common/queue.py:52`), so spare consumers appear with the spare's
hostname.

Touched files in this repo:
- `compose.yaml` — added `127.0.0.1:5432:5432` to postgres, `127.0.0.1:6379:6379` to redis (loopback only, see comments).
- `compose.sidecar.yaml` — minimal sidecar compose for the spare.
- `.env.sidecar.example` — env template for the spare.
- `.gitignore` — `.env.sidecar` excluded.

---

## 0. Pre-requisite ordering — read this first

The Redis byte-cap fix on `src/common/queue.py:155-164` is not strictly
required to bring the spare online with 1 replica, but **scaling the
spare beyond `--scale=1` without that fix accelerates the
`stream:extract` OOM**. The current count-based caps assume small entry
payloads; raw HTML in `stream:extract` blows the byte budget long before
the count cap is hit. Recommended order:

1. Ship the queue.py byte-cap fix (separate change).
2. Bring the spare up with `--scale=1` per this runbook.
3. Monitor `INFO memory used_memory_human` on the Pi's Redis for 24 h.
4. Only then scale to `--scale=2`.

---

## 1. Spare-machine pre-requisites (install)

Linux x86_64. This deployment targets **Pop OS 24.04 desktop**
(Ubuntu 24.04 base, same WiFi as the Pi, dedicated user
`remote_lakshit_gupta`). LAN-reachable but the Pi -> spare path is
never used — only `spare -> Pi` outbound autossh tunnel. Ubuntu / Debian
commands below apply verbatim; Fedora set noted at the end.

### 1.1 Sizing

- CPU: 2 cores minimum, 4 comfortable.
- RAM: 4 GB minimum (1.5 GB × 2 replicas + Xvfb + Firefox spikes), 8 GB recommended.
- Disk: 15 GB free (`docker/camoufox.Dockerfile` produces ~3.5 GB image).
- Network: WiFi acceptable; tunnel resilience handled by autossh.

### 1.2 Packages

**Ubuntu / Debian:**
```bash
sudo apt-get update
sudo apt-get install -y \
  ca-certificates curl git openssh-client autossh \
  redis-tools postgresql-client netcat-openbsd chrony
curl -fsSL https://get.docker.com | sudo sh
sudo apt-get install -y docker-compose-plugin
```

**Fedora:**
```bash
sudo dnf install -y ca-certificates curl git openssh-clients autossh \
  redis nc postgresql chrony dnf-plugins-core
sudo dnf config-manager --add-repo https://download.docker.com/linux/fedora/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo systemctl enable --now docker
```

### 1.3 Host hardening

```bash
# Disable suspend/sleep — Redis stream-ID time arithmetic + tunnel survival
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target

# Time-sync (Redis idle-claim math + Postgres NOW() correlation)
sudo systemctl enable --now chrony
chronyc tracking   # verify

# Workload user in the docker group (use whatever you named the spare user)
sudo usermod -aG docker remote_lakshit_gupta
# log out + back in as remote_lakshit_gupta so the group takes effect

# Docker log rotation (avoids unbounded /var/lib/docker/containers/ growth)
sudo tee /etc/docker/daemon.json <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "50m", "max-file": "3" }
}
EOF
sudo systemctl restart docker
```

---

## 2. Pi-side prep (one-time)

### 2.1 Compose changes (already in repo)

`compose.yaml` now publishes Redis and Postgres on `127.0.0.1` only. On
the Pi:

```bash
cd /home/dietpi/coding/Cartograph
git pull
docker compose up -d --force-recreate postgres redis
ss -ltnp | grep -E '127\.0\.0\.1:(5432|6379)'
```

Both bindings MUST show `127.0.0.1`, never `0.0.0.0` or `*:`. If either
shows `0.0.0.0`, abort and check the compose syntax. Verify nothing
listens on a LAN-routable IP:

```bash
ss -ltnp | grep -E ':5432|:6379'
# Only 127.0.0.1 lines should appear.
```

### 2.2 Dedicated tunnel user

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin carto-tunnel
sudo mkdir -p /home/carto-tunnel/.ssh
sudo chmod 700 /home/carto-tunnel/.ssh
sudo touch /home/carto-tunnel/.ssh/authorized_keys
sudo chmod 600 /home/carto-tunnel/.ssh/authorized_keys
sudo chown -R carto-tunnel:carto-tunnel /home/carto-tunnel/.ssh
```

`/usr/sbin/nologin` blocks interactive login; `ssh -N -L …` (no command)
still works because sshd handles port forwarding before invoking the
login shell.

### 2.3 sshd configuration

If `ss -ltnp | grep sshd` shows sshd bound to Tailscale only (per
CLAUDE.md baseline), add LAN binding for the tunnel user. Create
`/etc/ssh/sshd_config.d/10-carto-tunnel.conf`:

```
# Keep the existing Tailscale ListenAddress lines; ADD a LAN bind.
ListenAddress 192.168.1.240

AllowTcpForwarding yes
GatewayPorts no
PermitTunnel no
X11Forwarding no
ClientAliveInterval 30
ClientAliveCountMax 4

Match User carto-tunnel Address 192.168.1.0/24
    AllowTcpForwarding yes
    PermitOpen 127.0.0.1:6379 127.0.0.1:5432
    ForceCommand /bin/false
    AllowAgentForwarding no
    PermitTTY no
    X11Forwarding no

Match User carto-tunnel Address *,!192.168.1.0/24
    DenyUsers carto-tunnel
```

Then:
```bash
sudo sshd -t                       # validate
sudo systemctl reload ssh
```

(Optional) firewall the SSH port to LAN-only:
```bash
sudo ufw allow from 192.168.1.0/24 to 192.168.1.240 port 22 proto tcp
```

### 2.4 Extract minimal credentials

Run on the Pi, in the repo where `secrets.yaml` lives:

```bash
sops -d --extract '["redis_password"]'             secrets.yaml
sops -d --extract '["postgres_user"]'              secrets.yaml
sops -d --extract '["postgres_password"]'          secrets.yaml
sops -d --extract '["postgres_db"]'                secrets.yaml
sops -d --extract '["libsodium_master_key_hex"]'   secrets.yaml
```

Move those five values to the spare via password manager / Signal /
in-person. Do **NOT** copy `~/.config/sops/age/keys.txt` to the spare —
spare must not hold the master decryption key.

---

## 3. Spare-machine setup

As the workload user on the spare (`remote_lakshit_gupta` or whatever you named
it):

### 3.1 SSH key

```bash
ssh-keygen -t ed25519 -f ~/.ssh/carto_tunnel_ed25519 \
  -C "carto-tunnel@$(hostname)" -N ""
cat ~/.ssh/carto_tunnel_ed25519.pub
```

Paste that public key into `/home/carto-tunnel/.ssh/authorized_keys` on
the Pi, **prefixed** with the restrictions:

```
restrict,port-forwarding,permitopen="127.0.0.1:6379",permitopen="127.0.0.1:5432",command="/bin/false" ssh-ed25519 AAAA…<your pubkey>… carto-tunnel@<spare-hostname>
```

Smoke-test from the spare (will hang silently on success — Ctrl-C):
```bash
ssh -i ~/.ssh/carto_tunnel_ed25519 -N \
  -L 127.0.0.1:6379:127.0.0.1:6379 \
  -L 127.0.0.1:5432:127.0.0.1:5432 \
  -o ExitOnForwardFailure=yes \
  carto-tunnel@192.168.1.240
```

Accept the host key on first connect; it's now in `~/.ssh/known_hosts`
so autossh won't prompt.

### 3.2 Clone repo + env file

```bash
cd ~
git clone <repo-url> Marked_Path
cd Marked_Path
git checkout main

cp .env.sidecar.example .env.sidecar
chmod 600 .env.sidecar
# Edit .env.sidecar — paste the five values from step 2.4
${EDITOR:-nano} .env.sidecar
```

### 3.3 autossh systemd unit

Create `/etc/systemd/system/carto-tunnel.service` (root edit):

```ini
[Unit]
Description=Persistent SSH tunnel to Pi (Redis + Postgres forwards)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=remote_lakshit_gupta
Group=remote_lakshit_gupta

# Disable autossh's monitor port — rely on SSH keepalives instead.
# They survive WiFi roams and NAT timeouts better than autossh's
# port-bounce probe.
Environment=AUTOSSH_GATETIME=0
Environment=AUTOSSH_PORT=0

ExecStart=/usr/bin/autossh -N \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=/home/remote_lakshit_gupta/.ssh/known_hosts \
  -o IdentitiesOnly=yes \
  -i /home/remote_lakshit_gupta/.ssh/carto_tunnel_ed25519 \
  -L 127.0.0.1:6379:127.0.0.1:6379 \
  -L 127.0.0.1:5432:127.0.0.1:5432 \
  carto-tunnel@192.168.1.240

Restart=always
RestartSec=10s

NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/remote_lakshit_gupta/.ssh
PrivateTmp=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true

[Install]
WantedBy=multi-user.target
```

Substitute `remote_lakshit_gupta` and its `$HOME` path if your workload user has
a different name.

Activate:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now carto-tunnel.service
sudo systemctl status carto-tunnel.service
journalctl -u carto-tunnel.service -f   # tail until tunnel reports up
```

### 3.4 Build the camoufox image

```bash
cd ~/Marked_Path
docker build -f docker/camoufox.Dockerfile -t marked_path-camoufox-worker:latest .
```

First build ~10-15 min on a normal x86 laptop (Firefox download, Xvfb,
uv sync). Subsequent rebuilds hit cache.

### 3.5 Bring sidecar up

```bash
docker compose -f compose.sidecar.yaml up -d
docker compose -f compose.sidecar.yaml logs -f camoufox-worker-spare
```

Watch for `redis_connected` and a `consume` loop on `stream:fetch`.

---

## 4. Verification

### 4.1 Tunnel up (spare)

```bash
sudo systemctl is-active carto-tunnel       # active
nc -vz 127.0.0.1 6379                       # succeeded
nc -vz 127.0.0.1 5432                       # succeeded
ss -lntp | grep -E '127\.0\.0\.1:(6379|5432)'   # ssh process owns both
```

### 4.2 Redis + Postgres pings (spare)

```bash
set -a; source ~/Marked_Path/.env.sidecar; set +a

redis-cli -h 127.0.0.1 -a "$REDIS_PASSWORD" --no-auth-warning ping
# → PONG
redis-cli -h 127.0.0.1 -a "$REDIS_PASSWORD" --no-auth-warning XLEN stream:fetch
# → some integer

PGPASSWORD="$POSTGRES_PASSWORD" psql \
  "host=127.0.0.1 port=5432 user=$POSTGRES_USER dbname=$POSTGRES_DB sslmode=disable" \
  -c "SELECT count(*) FROM identities;"
# → row count
```

### 4.3 Consumer-group membership (run on Pi)

```bash
RP=$(sops -d --extract '["redis_password"]' secrets.yaml)
docker exec cartograph-redis-1 redis-cli -a "$RP" --no-auth-warning \
  XINFO CONSUMERS stream:fetch g:crawlers
```

Should list the spare's consumer (`<spare-hostname>-<pid>`) alongside
the 3 Pi consumers.

### 4.4 End-to-end smoke (run on Pi)

Capture baseline, temporarily stop Pi-side camoufox so any browser work
MUST flow through spare:

```bash
docker exec cartograph-redis-1 redis-cli -a "$RP" --no-auth-warning XLEN stream:extract
# note the number
docker compose stop camoufox-worker
# wait 60-120s for scheduler emit + spare consume
docker exec cartograph-redis-1 redis-cli -a "$RP" --no-auth-warning XLEN stream:extract
# should have increased
docker compose start camoufox-worker
```

### 4.5 Identity-vault sanity (run on Pi)

No identity double-leased:
```sql
SELECT identity_id, count(*) AS n
FROM identity_checkouts
WHERE returned_at IS NULL AND expires_at > NOW()
GROUP BY identity_id HAVING count(*) > 1;
```

Must return zero rows. `FOR UPDATE SKIP LOCKED` in
`src/common/identity_vault.py:162-170` enforces this across hosts since
both workers share one Postgres.

---

## 5. Daily operations (on spare)

| Action | Command |
|---|---|
| Start | `docker compose -f compose.sidecar.yaml up -d` |
| Stop | `docker compose -f compose.sidecar.yaml down` |
| Restart worker only | `docker compose -f compose.sidecar.yaml restart` |
| Tail logs | `docker compose -f compose.sidecar.yaml logs -f --tail 100` |
| Update code | `git pull && docker compose -f compose.sidecar.yaml up -d --build` |
| Tunnel status | `sudo systemctl status carto-tunnel` |
| Tunnel restart | `sudo systemctl restart carto-tunnel` |
| Scale up (after cap fix ships!) | `docker compose -f compose.sidecar.yaml up -d --scale camoufox-worker-spare=2` |

---

## 6. Failure modes

| Failure | What happens | Recovery |
|---|---|---|
| Tunnel drops mid-task | autossh detects within ~90s (ServerAliveInterval × Max), systemd restarts. Worker's in-flight Redis op fails; message stays unACK'd; `XAUTOCLAIM` reassigns after 5 min idle. | Auto. |
| Pi reboots | autossh keeps retrying every 10s. Once Pi sshd is up, tunnel re-establishes; worker reconnects via `src/common/queue.py:205-208` exception loop. | Auto. |
| Spare WiFi drops | Same as tunnel drop. | Auto on reconnect. |
| Spare disk fills | Logs go to journald + Docker JSON log driver (50m × 3 rotation, see §1.3). Build cache can grow; `docker system prune` to clean. | Manual; alert at 80% via `df -h /var/lib/docker`. |
| Pi sshd host key changes (Pi reflash) | autossh refuses to connect (StrictHostKeyChecking=yes). | `ssh-keygen -R 192.168.1.240` on spare, re-fingerprint via one interactive `ssh -i … carto-tunnel@192.168.1.240`. |
| Identity double-lease attempt | Caught by `FOR UPDATE SKIP LOCKED` in identity_vault. Loser retries with the next identity. | Auto. |
| Time drift on spare | Redis Stream IDs come from Redis's clock, not the client's, so consumer-claim math is unaffected. App-level `NOW()` writes come from Postgres on the Pi. chrony handles client clock for app-level timestamps. | `chronyc tracking` if suspect. |

---

## 7. Rollback

If the sidecar misbehaves or you want to remove it:

1. **Stop sidecar** (spare): `docker compose -f compose.sidecar.yaml down`.
2. **Stop tunnel** (spare): `sudo systemctl disable --now carto-tunnel.service`.
3. **Revoke key** (Pi): delete the spare's line from
   `/home/carto-tunnel/.ssh/authorized_keys`. Tunnel cannot
   re-establish.
4. (Optional) **Undo Pi loopback ports**: revert the `ports:` blocks in
   `compose.yaml` for postgres + redis, then
   `docker compose up -d --force-recreate postgres redis`. Returns the
   Pi to the strict "no host port published" CLAUDE.md baseline.
5. (Optional) **Remove tunnel user** (Pi): `sudo userdel -r carto-tunnel`.

Pi-side stack continues unchanged throughout. Its existing 3 camoufox
replicas consume what would have gone to the spare. The spare's removal
is invisible to every other service.

---

## 8. Open follow-ups (deferred)

- **FlareSolverr access from spare.** If T1 tasks routed to the spare
  hit Cloudflare, the worker calls `flaresolverr_url` and fails. Two
  fixes when needed: (a) defer — leave `FLARESOLVERR_URL` unset; spare
  skips CF-protected sources. (b) Add `127.0.0.1:8191:8191` ports to
  flaresolverr in compose.yaml, `permitopen="127.0.0.1:8191"` to
  authorized_keys, `-L 127.0.0.1:8191:127.0.0.1:8191` to the autossh
  unit, `FLARESOLVERR_URL=http://127.0.0.1:8191/v1` to `.env.sidecar`.
- **Image registry.** Spare builds the image from source. Pi and spare
  can drift if rebuilt separately. Future: publish to GHCR / R2 and
  pull on both, or extend `scripts/ship_to_pi.sh` to also push to the
  spare.
- **Centralized observability.** Spare's container logs go to its own
  journald, not the Pi's `/var/lib/agent/logs`. Acceptable for v1;
  revisit when adding a third host.
- **3+ replicas** require revisiting `mem_limit`, `pids_limit`, and the
  identity-vault pool size before scaling.

---

## 9. Phase 4 — `apply-browser-worker` (Internshala auto-apply)

The same sidecar host also runs `apply-browser-worker` (defined in
`compose.sidecar.yaml`). This service is the ThinkPad-side leg of the
auto-apply pipeline. The Pi's `applier-worker` consults
`src/application/policy.py:should_auto_submit()` and, for whitelisted
`(method, source)` pairs, publishes a `BrowserApplyTask` onto
`stream:apply_browser`. The sidecar consumes it from the SSH-tunneled
Redis, decrypts the Internshala session cookie locally, drives
camoufox to fill the Easy Apply modal, and either submits or stops
(dry-run mode) and screenshots the result.

### 9.1 Additional `.env.sidecar` requirement

The sidecar now decrypts session cookies on its own. Step 2.4 already
includes `LIBSODIUM_MASTER_KEY_HEX` — confirm it is set in
`.env.sidecar`. Without it, `apply-browser-worker` boots, emits
`identity_decrypt_failed`, and crash-loops.

### 9.2 No additional Pi-side changes

`stream:apply_browser` and `stream:apply_browser_result` are declared
in `src/common/queue.py` (Phase 4). The Pi's existing `applier-worker`
and the new `apply-result-worker` (added to `compose.yaml`) handle the
producer + result-drain sides. No new SSH forwards are required — the
two new streams ride the same `127.0.0.1:6379` tunnel.

### 9.3 First-time recon (one-shot, before code lands)

Before `src/application/submitters/internshala_browser.py` selectors
are committed, sit on the spare and capture the live Easy Apply DOM
from one manual application:

```bash
# As remote_lakshit_gupta, with a real browser (not the sidecar
# container — devtools needed):
xdg-open https://internshala.com/internship/detail/<some-internship-id>
# Click Easy Apply → modal opens.
# Devtools → Elements → record:
#   - Easy Apply button selector
#   - Resume upload input selector (input[type=file])
#   - Cover letter textarea selector
#   - Each custom-question textarea selector
#   - Submit button selector
#   - Success banner / toast selector
# Devtools → Network → record the XHR endpoint + status code on submit
# Paste all selectors into INTERNSHALA_SELECTORS in
# src/application/submitters/internshala_browser.py.
```

This is intentionally manual and one-shot. Internshala changes their
DOM ~once a quarter; selector drift surfaces as `apply_failed` results
in Discord and is fixed by re-running this recon and patching the
constant.

### 9.4 Bring up the apply worker

```bash
cd ~/Marked_Path
git pull
docker compose -f compose.sidecar.yaml up -d --build apply-browser-worker
docker compose -f compose.sidecar.yaml logs -f --tail 100 apply-browser-worker
```

Expect on first boot:
- `redis_connected url=127.0.0.1`
- `consumer_group_ready stream=stream:apply_browser group=g:browser_appliers`
- Idle `xreadgroup_idle` heartbeat until the Pi publishes a task.

### 9.5 Dry-run verification (after queue end-to-end is live)

Drive through the steps in `docs/runbooks/internshala_auto_apply_dryrun.md`.

### 9.6 Daily ops additions

| Action | Command |
|---|---|
| Restart only the apply worker | `docker compose -f compose.sidecar.yaml restart apply-browser-worker` |
| Tail apply worker | `docker compose -f compose.sidecar.yaml logs -f apply-browser-worker` |
| See queue depth from spare | `redis-cli -h 127.0.0.1 -a "$REDIS_PASSWORD" --no-auth-warning XLEN stream:apply_browser` |

### 9.7 Threat model addendum

- `LIBSODIUM_MASTER_KEY_HEX` now lives on the spare. The spare must
  therefore be treated as Pi-equivalent in terms of physical access,
  full-disk encryption (LUKS recommended on the Pop OS install), and
  network exposure. Do not run other untrusted workloads on the spare.
- The base64 PDFs travelling in `stream:apply_browser` are inside the
  SSH tunnel; once decoded on the spare they land in tmpfs `/tmp/apply/`
  and are deleted after submit (or sidecar restart wipes tmpfs).
- Screenshots in `stream:apply_browser_result` may contain the user's
  name + Internshala UI; treat the Discord channel that surfaces them
  (`#auto-apply-dryrun` and `#✅-applied`) as private.
