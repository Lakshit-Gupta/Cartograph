# Cartograph

> Autonomous 24/7 opportunity mapper. Discord bot identity: **Hop**.

Autonomous 24/7 job / internship / fellowship / freelance pipeline. Runs on a Raspberry Pi 5 (DietPi, ARM64, 8GB). Crawls 28+ sources, ranks against the user profile, delivers a daily Discord digest, supports apply / skip / snooze via buttons + slash commands, tracks outcomes via Gmail IMAP, learns from feedback.

> Success metric: first paid internship OR first paid freelance gig within 30 days of go-live.

Authoritative documents:
- `CLAUDE.md` — working summary every dev session should read first.
- `/home/lakshit_gupta/.claude/plans/virtual-splashing-pine.md` — full plan with rationale.

---

## Repo layout

```
cartograph/
├── pyproject.toml         uv-managed deps
├── compose.yaml           Docker Compose stack
├── secrets.yaml.example   SOPS template (encrypt to secrets.yaml on the Pi)
├── Makefile               make help
├── docker/                Dockerfiles + postgres config + init SQL
├── migrations/            V001..V005 SQL migrations
├── config/                profile, sources YAMLs, LLM prompts, routing rules
├── src/
│   ├── common/            db / queue / types / identity_vault / llm / metrics / logger / secrets
│   ├── fetchers/          http (T0) / flaresolverr (T1) / browser (T2 = camoufox)
│   ├── extractors/        T0 regex / T1 selectors / T2 LLM / dedup
│   ├── sources/           ats / rss / github_markdown / hn / reddit / fellowship / india / freelance
│   ├── ranker/            formula + embeddings + llm_rerank + feedback
│   ├── notifiers/         discord (23 slash commands, embeds, handlers) / obsidian / email
│   ├── gmail_watcher/     IMAP IDLE + LLM classifier + state writer + Upwork parser
│   ├── application/       resume_tailor / cover_letter / sender
│   ├── workers/           scheduler / crawler / extractor / ranker / notifier / gmail / identity_warmup
│   ├── api/               FastAPI (/health, /metrics, /admin)
│   └── cli/               `mp` admin command (migrate, seed-sources, sources, identity, opps)
├── scripts/               bootstrap.sh / backup.sh / restore_drill.sh / pretest_cf.sh
├── docs/runbooks/         pi_recovery.md / source_quarantine.md / identity_ban.md
└── grafana/dashboards/    agent_jobs.json
```

Two non-negotiables:

- **No cross-subsystem imports.** Subsystems talk via Redis Streams. `src/common/` is the only universal import target.
- **Power-fail safety.** Postgres `synchronous_commit=on`, `full_page_writes=on`, WAL archive every 5 min. Redis `appendfsync everysec`. No UPS — durability config replaces it. Do **not** weaken.

---

## Bring-up order (on the Pi only)

> Important: **do not run these on a non-Pi machine.** Several steps (swap, fsck, `/mnt/storage` paths, port reservations) assume the deployment host.

```bash
# 1. Clone
git clone <repo> /home/$USER/coding/cartograph
cd /home/$USER/coding/cartograph

# 2. Host prep — swap, fonts, WAL dirs, fsck flag
CARTOGRAPH_PI_CONFIRM=1 sudo bash scripts/bootstrap.sh

# 3. SOPS + secrets
age-keygen -o ~/.config/sops/age/keys.txt
# Get pubkey (last line of file output above) → use it below
cp secrets.yaml.example secrets.yaml
$EDITOR secrets.yaml        # fill in all REPLACE values
sops --encrypt --age <pubkey> --in-place secrets.yaml

# 4. Profile assets (user-owned)
$EDITOR config/profile/resume.json
$EDITOR config/profile/skills.yaml
$EDITOR config/profile/comp_floors.yaml
$EDITOR config/profile/prefs.yaml

# 5. Build + start
make up                     # sops exec-env + docker compose up -d

# 6. DB schema + seed
make migrate
make seed

# 7. Verify
docker compose ps           # all services Up
curl -s http://localhost:9090/health
curl -s http://localhost:9090/metrics | head
```

---

## Daily workflow (after go-live)

- Read the **daily digest** in `#📰-daily-digest` (or run `/digest now`).
- Click **Apply** / **Skip** / **Snooze** / **Pin** on each opportunity.
- Watch **freelance speed lane** in `#⚡-priority-push`. Edit the LLM-drafted proposal in the modal, then **Send**.
- Glance at `#📬-responses` and `#🎤-interviews` daily.
- 9pm IST: if `applied_today < 5`, you'll be @mentioned in `#🔔-alerts`.

Useful CLI:

```bash
docker compose run --rm tools python -m src.cli.main opps recent --limit 30
docker compose run --rm tools python -m src.cli.main sources list
docker compose run --rm tools python -m src.cli.main identity status
docker compose run --rm tools python -m src.cli.main identity gen-master-key
```

---

## Slash commands (23)

```
/budget set|today|status
/digest now|preview|schedule
/apply <opp_id>
/skip <opp_id>
/snooze <opp_id> <days>
/pin <opp_id>
/status
/source list|pause|resume|add
/identity status|update
/cost today|cap
/followup <opp_id>
/explain <opp_id>
/export <range>
/review                   # v2 — dark-source candidates
```

---

## Day-14 verification (12 checks)

See `CLAUDE.md` § Verification. Hard pass criteria:

1. Power-cut → Postgres `pg_amcheck` clean.
2. Redis AOF replay clean.
3. `bash scripts/restore_drill.sh` passes.
4. `cf_clearance_solve_rate > 0.7` after 10 hits to a CF source.
5. End-to-end happy path: Greenhouse → opp row → score row → embed posted → Apply click → application row + applied thread.
6. Reaction handler matches button handler.
7. `/status`, `/budget today 30`, `/source list` work.
8. Gmail watcher transitions opp state on rejection email.
9. 9pm nudge fires when applied_today < target.
10. Existing Prometheus scrapes our `/metrics`; Grafana panels render.
11. Cost cap refuses next LLM call when inflated past $3/day.
12. Banning one identity cascades sibling quarantine within 5min.

---

## Open user-action items (before / during Phase 1)

| # | Item                                                                        | Window           |
|---|-----------------------------------------------------------------------------|------------------|
| 1 | ~~Pick jobs bot name~~ — **locked: Hop** (Grace Hopper, first compiler 1952) | Done             |
| 2 | OpenRouter API key                                                          | Day -1           |
| 3 | New Discord application + bot + invite to JOBS category only                | Day -3           |
| 4 | Cloudflare Email Routing catch-all + aliases (jobs+wellfound@, etc.)        | Day -2           |
| 5 | Master resume → JSON at `config/profile/resume.json`                        | Day -3 → 0       |
| 6 | Skill matrix YAML at `config/profile/skills.yaml`                           | Day -3 → 0       |
| 7 | Comp floors at `config/profile/comp_floors.yaml`                            | Day -3 → 0       |
| 8 | Identity warmup on platforms (10 min/day, real clicks)                      | Day -3 → 0       |
| 9 | Resend signup + verify sender domain                                        | Day -2           |
| 10 | Google Cloud project + Gmail OAuth client (or app passwords)               | Day -2           |
| 11 | Telegram `api_id` / `api_hash`                                             | Before Day 13    |
| 12 | Worker Gmail inbox (e.g. `upwork-worker@yourdomain.tld`) + app password   | Before Day 13    |

---

## Phase gate

Until first paid earnings: **one new feature per week max**. Only features that demonstrably improve apply rate or response rate are allowed. Phase 2 / 3 / 4 / 5 roadmaps live in `CLAUDE.md`.
