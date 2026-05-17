# cartograph — Pi 5 deploy

End-to-end deploy of Phase 1 on a fresh DietPi (or any Debian-based Linux) Raspberry Pi 5. Estimated time: **45-90 min** assuming you've completed Phase-0 user actions in advance.

> Do **NOT** run any of this on your dev laptop. The bootstrap script gates on `CARTOGRAPH_PI_CONFIRM=1` so accidental runs no-op.

---

## 0. Phase-0 prerequisites (do these BEFORE touching the Pi)

These are signups + content. None require the Pi.

| # | Item | Where | Time |
|---|---|---|---|
| 1 | OpenRouter API key | https://openrouter.ai | 10m |
| 2 | New Discord application named **Hop** + bot token | https://discord.com/developers/applications | 10m |
| 3 | Discord server + categories + channels (see `CLAUDE.md § Discord server layout`) | your existing server | 20m |
| 4 | Cloudflare Email Routing — catch-all + aliases (`jobs+wellfound@`, `jobs+cuvette@`, `jobs+contra@`, `jobs+unstop@`, `applications@`, `upwork-worker@`, `bot@`) | https://dash.cloudflare.com | 20m |
| 5 | Resend account + verify sender domain | https://resend.com | 15m |
| 6 | Google Cloud project + Gmail API + OAuth credentials (web app type) → obtain refresh token via gcloud or oauth playground | https://console.cloud.google.com | 30m |
| 7 | Reddit script-app: client_id + client_secret | https://reddit.com/prefs/apps | 5m |
| 8 | Telegram api_id + api_hash | https://my.telegram.org | 10m |
| 9 | Cloudflare R2 bucket `agent-jobs-backups` + scoped token | https://dash.cloudflare.com → R2 | 10m |
| 10 | Master resume → JSON at `config/profile/resume.json` | local | 1-2h |
| 11 | Skill matrix + comp floors + prefs (`config/profile/{skills,comp_floors,prefs}.yaml`) | local | 30m |
| 12 | Identity warmup — manual logins on Wellfound, Cuvette, Unstop, Contra (start Day -3) | platforms | 10m/day × 4 days |

---

## 1. Pi host prep (one-time)

```bash
# SSH into Pi
ssh you@pi.local

# Clone
git clone <YOUR_REPO_URL> ~/coding/cartograph
cd ~/coding/cartograph

# Install Docker + Docker Compose if not already (DietPi ships them; otherwise):
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && newgrp docker

# Install uv (optional — only if you'll run the CLI directly outside containers)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install SOPS + age
sudo apt-get install -y age
curl -LO https://github.com/getsops/sops/releases/latest/download/sops-v3.9.1.linux.arm64
sudo install sops-v3.9.1.linux.arm64 /usr/local/bin/sops
rm sops-v3.9.1.linux.arm64

# rclone (for R2 backups)
sudo apt-get install -y rclone
mkdir -p /etc/cartograph
sudo nano /etc/cartograph/rclone.conf
# Paste:
#   [r2]
#   type = s3
#   provider = Cloudflare
#   access_key_id = <r2 access key>
#   secret_access_key = <r2 secret>
#   endpoint = https://<account_id>.r2.cloudflarestorage.com
#   region = auto

# age recipients file (one line, public age key)
sudo nano /etc/cartograph/age_recipients.txt   # one age1... key per line

# Run the Pi-specific bootstrap (creates swap, fonts, log dirs, WAL archive dir,
# fsck flag, installs the cron entries)
CARTOGRAPH_PI_CONFIRM=1 sudo bash scripts/bootstrap.sh
```

`bootstrap.sh` step 7 calls `install_cron.sh` which writes 3 entries to your crontab:
- nightly backup at 03:30 UTC
- weekly restore drill at Sunday 04:00 UTC
- nightly `pg_amcheck` at 05:00 UTC

Verify with `crontab -l | grep cartograph_cron`.

---

## 2. SOPS secrets

```bash
# Generate the age master key (NEVER commit; store offline duplicate)
age-keygen -o ~/.config/sops/age/keys.txt
chmod 600 ~/.config/sops/age/keys.txt
AGE_PUBKEY=$(grep -oP 'public key: \Kage1\S+' ~/.config/sops/age/keys.txt)
echo "Pubkey: $AGE_PUBKEY"

# Copy template, fill in EVERY REPLACE value
cp secrets.yaml.example secrets.yaml
nano secrets.yaml
# Fields to fill:
#   postgres_password (strong) , redis_password (strong)
#   openrouter_api_key , resend_api_key , resend_from_email
#   discord_bot_token , discord_guild_id
#   discord_channel_* (14 numeric channel IDs from your Discord server)
#   telegram_api_id , telegram_api_hash
#   reddit_client_id , reddit_client_secret
#   gmail_oauth_* (4 fields) , gmail_user , gmail_worker_user , gmail_worker_app_password
#   r2_* (4 fields)
#   libsodium_master_key_hex — generate ONCE with:
#       docker compose run --rm tools python -m src.cli.main identity gen-master-key

# Encrypt in place
sops --encrypt --age "$AGE_PUBKEY" --in-place secrets.yaml

# Verify it stays encrypted on disk
head -3 secrets.yaml   # should start with sops: not your real values
```

Commit the encrypted `secrets.yaml` to git. **Never** commit the unencrypted form. The `.pre-commit-config.yaml` has a guard that rejects un-encrypted commits.

---

## 3. Bring up the stack

```bash
make up               # = sops exec-env secrets.yaml 'docker compose up -d'
make ps               # verify all services are Up
docker compose logs -f --tail=50 postgres   # confirm "database system is ready"
```

Expected containers (`docker compose ps`):

```
postgres            healthy
redis               healthy
flaresolverr
camoufox-worker     (3 replicas)
jobs-scheduler
crawler-worker      (3 replicas)
extractor-worker
ranker-worker
notifier-discord
gmail-watcher
api-service
applier-worker
identity-warmup
```

If any service restarts in a loop, check `docker compose logs <service>`.

---

## 4. Schema + sources seed

```bash
make migrate          # applies V001..V006 in order
make seed             # re-runs V003 source-seed (idempotent)
```

Sanity check:

```bash
docker compose exec postgres psql -U marked -d marked -c \
  "SELECT slug, status, fetch_freq_minutes FROM sources ORDER BY priority DESC LIMIT 10;"
```

You should see 30 rows, all `active`.

---

## 5. Profile + identity load

```bash
# Profile YAMLs are read directly off disk by the ranker — just leave the
# filled `config/profile/*.yaml` in place. The ranker_worker upserts the
# profile row into Postgres on first tick.

# Identity vault — for each platform you've warmed up manually:
docker compose run --rm tools python -m src.cli.main identity add \
  --platform internshala --label "warm-1" \
  --credentials-json '{"username":"jobs+internshala@yourdomain.tld","password":"..."}' \
  --cookies-json '{}' \
  --email-alias jobs+internshala@yourdomain.tld

# Repeat for cuvette, unstop, contra, reddit, wellfound (if you exported cookies)
docker compose run --rm tools python -m src.cli.main identity status
```

---

## 6. First-digest smoke

```bash
# Watch the pipeline in real time across all workers
docker compose logs -f --tail=20 scheduler crawler-worker extractor-worker ranker-worker notifier-discord

# In Discord, in your test channel:
/digest now
/source list      # 30 sources, all active
/status           # pipeline overview
/identity status  # your loaded identities
/cost today       # should be near $0
```

Within 60 s of `/digest now`, you should see an embed in `#📰-daily-digest`.

---

## 7. Day-14 verification (12 checks)

Run these in order. The plan's exit criteria for Phase 1.

| # | Check | Command |
|---|---|---|
| 1 | Postgres power-fail durability | unplug Pi during heavy crawl, replug → `docker compose exec postgres pg_amcheck -U marked marked --verbose` returns 0 |
| 2 | Redis AOF replay | same test → `docker compose logs redis | grep "AOF loaded"` |
| 3 | Restore drill from R2 | `make restore-drill` |
| 4 | CF clearance > 70% | `bash scripts/pretest_cf.sh fl_contra 10` |
| 5 | End-to-end happy path | `/digest now` → click Apply on first card → forum thread appears in `#✅-applied` within 30 s |
| 6 | Reaction handler == button handler | react ✅ to an opp embed → state mutates identically |
| 7 | Slash commands | `/status`, `/budget today 30`, `/source list` all return non-error |
| 8 | Gmail watcher classifies a rejection | send a test "we have decided not to move forward" email to your monitored inbox → opp state transitions to `rejected` |
| 9 | 9pm nudge | wait until 21:00 IST; if `applied_today < 5` → @mention in `#🔔-alerts` |
| 10 | Prometheus scrape | `curl http://<pi>:9090/metrics | grep cf_clearance_solve_rate` returns a value |
| 11 | Cost-cap kill switch | `docker compose exec postgres psql -U marked -d marked -c "INSERT INTO daily_spend(date, source_id, tier, request_count, cents_spent) VALUES (CURRENT_DATE, NULL, 0, 1, 1500);"` → next LLM call raises `CostCapReached` |
| 12 | Identity ban cascade | `UPDATE identities SET ban_status='banned' WHERE id=X` → sibling identities with same `fingerprint_id` auto-quarantine within 5 min |

12/12 pass = Phase 1 production-ready for solo use.

---

## 8. Monitoring after go-live

- Grafana dashboard: load `grafana/dashboards/agent_jobs.json` into your existing Grafana via dashboard import (UID `cartograph-agent-jobs`).
- Prometheus target: add `<pi>:9090` to your Prometheus `scrape_configs`.
- Alerts route to `#🔔-alerts` automatically via the notifier worker.

---

## Common Pi-side problems

| Symptom | Fix |
|---|---|
| `READY event absent >5min` | `docker compose restart notifier-discord` |
| `cf_clearance_solve_rate < 0.5` | `bash scripts/pretest_cf.sh <source_slug> 20` to warm cache; check `cf_clearance_cache` table |
| Source auto-quarantined | `docs/runbooks/source_quarantine.md` |
| Identity ban cascade | `docs/runbooks/identity_ban.md` |
| Pi crash / power loss | `docs/runbooks/pi_recovery.md` |
| Postgres won't start | `docker compose logs postgres` → if WAL corrupt, restore from latest R2 backup |
| Daily LLM cost > $3 | `/cost cap 5` to raise temporarily; investigate which kind is hot via Grafana `llm_cost_usd_total{kind,model}` |
