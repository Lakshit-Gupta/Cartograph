# Marked_Path — Project Context for Claude

> Source of truth: `/home/lakshit_gupta/.claude/plans/virtual-splashing-pine.md`. Read that plan when full detail is needed. This file is the working summary every session loads.

---

## What this is

Autonomous 24/7 job / internship / fellowship / freelance pipeline running on a Raspberry Pi 5 (DietPi, ARM64, 8GB). Crawls 28+ sources, ranks against user profile, delivers a daily Discord digest, supports apply / skip / snooze via buttons + slash commands, tracks outcomes via Gmail IMAP, learns from feedback.

**Success metric**: first paid internship OR first $X freelance gig within 30 days of go-live.

**Coexists** with existing Pi services (untouched): Jellyfin, Prometheus, Grafana, Cloudflared, Tailscale, PiVPN.

---

## Project root (locked)

```
/home/lakshit_gupta/coding/Marked_Path/
```

Every build session begins with:

```bash
cd /home/lakshit_gupta/coding/Marked_Path/
```

All paths in plan/docs/code are relative to this root unless an absolute path is given. No `agent-jobs/` subfolder — root is flattened.

---

## Power-fail safety (NO UPS)

Pi may lose power at any time. Durability config is non-negotiable:

### Postgres (`postgresql.conf`)

```ini
synchronous_commit = on              # default — DO NOT set off
full_page_writes = on                # default — DO NOT disable
wal_compression = on
wal_level = replica
shared_buffers = 512MB
effective_cache_size = 2GB
work_mem = 16MB
maintenance_work_mem = 128MB
checkpoint_timeout = 15min
max_wal_size = 1GB
autovacuum_naptime = 5min
archive_mode = on
archive_command = 'test ! -f /mnt/storage/wal_archive/%f && cp %p /mnt/storage/wal_archive/%f'
archive_timeout = 300                # 5min RPO
```

### Redis (`redis.conf`)

```ini
appendonly yes
appendfsync everysec                 # 1s loss window; preserves SD card
maxmemory 200mb
maxmemory-policy noeviction          # producer blocks on full queue
requirepass <from SOPS>
rename-command FLUSHALL ""
rename-command CONFIG ""
```

### Host

- `tune2fs -c 1 /dev/mmcblk0p2` — fsck every boot.
- 4GB swap at `/swapfile`, persistent via `/etc/fstab`.
- WAL archive dir: `/mnt/storage/wal_archive` (external SD, `postgres:postgres`).
- Agent logs on disk, not tmpfs: `/var/lib/agent/logs`.

### Recovery procedure (`docs/runbooks/pi_recovery.md`)

1. fsck runs on boot.
2. Docker Compose restarts via `restart: unless-stopped`.
3. Postgres WAL replay (≤15 min).
4. Redis AOF replay (≤1s loss).
5. Crawler workers reclaim Redis stream entries via `XAUTOCLAIM` after 5min idle.
6. Notifier reconnects to Discord gateway with backoff.
7. Alert fires in `#🔔-alerts` if `READY` event absent >5min.

**Trade-off accepted**: ~10–20% write throughput hit vs. unsafe config. Acceptable for projected <5 GB/day.

---

## Folder structure (locked)

```
Marked_Path/
├── pyproject.toml              # uv-managed deps
├── compose.yaml                # Docker Compose (all containers)
├── secrets.yaml                # SOPS-encrypted (committed)
├── .env                        # bootstrap only, then empty
├── .gitignore
├── Makefile
├── README.md
│
├── docker/
│   ├── jobs-bot.Dockerfile     # main Python image
│   ├── camoufox.Dockerfile     # Firefox + Xvfb (separate, ~400MB heavier)
│   └── tools.Dockerfile        # migrations + scripts
│
├── migrations/                 # ordered SQL
│   ├── V001__core_schema.sql
│   ├── V002__reserved_names_v2.sql
│   ├── V003__sources_seed.sql
│   └── V004__opp_state_machine.sql
│
├── config/
│   ├── profile/                # user-supplied
│   │   ├── resume.json
│   │   ├── skills.yaml
│   │   ├── comp_floors.yaml
│   │   ├── prefs.yaml
│   │   ├── resume_variants/
│   │   └── cover_letters/
│   ├── sources/                # seed slugs per ATS
│   │   ├── greenhouse_slugs.yaml
│   │   ├── lever_slugs.yaml
│   │   ├── ashby_slugs.yaml
│   │   ├── workable_slugs.yaml
│   │   └── fellowships.yaml
│   ├── prompts/                # LLM prompts as files
│   │   ├── tier2_extractor.txt
│   │   ├── re_ranker.txt
│   │   ├── cover_letter.txt
│   │   ├── email_classifier.txt
│   │   └── source_classifier.txt
│   ├── discord_tags.yaml
│   ├── routing_rules.yaml
│   └── cf_tier_chain.yaml
│
├── src/
│   ├── common/                 # shared primitives — ONLY universal import target
│   │   ├── db.py               # asyncpg pool + tenant resolver
│   │   ├── queue.py            # Redis Streams wrapper
│   │   ├── types.py            # Opportunity, Identity, Source
│   │   ├── identity_vault.py   # libsodium per-row encryption
│   │   ├── llm.py              # OpenRouter client + cost ledger
│   │   ├── metrics.py          # Prometheus instrumentation
│   │   ├── logger.py
│   │   └── secrets.py          # SOPS env loader
│   ├── fetchers/
│   │   ├── base.py             # Fetcher Protocol
│   │   ├── http.py             # curl_cffi chrome131
│   │   ├── flaresolverr.py     # cookie broker client (T1)
│   │   ├── browser/            # subpackage — separate Docker image
│   │   │   ├── camoufox.py
│   │   │   ├── pool.py
│   │   │   ├── scaler.py       # Jellyfin-aware
│   │   │   ├── behavioral.py   # ghost-cursor
│   │   │   └── lifecycle.py    # kill+restart per 30 pages
│   │   ├── proxy.py            # ProxyResolver (no-op v1)
│   │   └── dispatcher.py       # tier-routing
│   ├── extractors/
│   │   ├── base.py
│   │   ├── tier0_regex.py
│   │   ├── tier1_selectors/
│   │   ├── tier2_llm.py        # LLM fallback, sandboxed
│   │   └── dedup.py
│   ├── sources/
│   │   ├── ats/                # Greenhouse/Lever/Ashby/Workable
│   │   ├── rss/
│   │   ├── github_markdown/
│   │   ├── hn_algolia.py
│   │   ├── reddit_forhire.py
│   │   ├── fellowship/
│   │   ├── india/              # Cuvette/Unstop/YC India/Inc42/YourStory
│   │   └── freelance/          # Contra/Upwork email/Telegram
│   ├── ranker/
│   │   ├── formula.py
│   │   ├── embeddings.py       # sentence-transformers MiniLM
│   │   ├── llm_rerank.py
│   │   └── feedback.py
│   ├── notifiers/
│   │   ├── base.py
│   │   ├── discord/
│   │   │   ├── bot.py
│   │   │   ├── commands/       # one file per slash command
│   │   │   ├── embeds/         # opp_card, digest_header, priority_push
│   │   │   ├── handlers/       # buttons, reactions, modals
│   │   │   ├── routing.py
│   │   │   └── voice.py        # microcopy
│   │   ├── obsidian.py
│   │   └── email.py            # Resend
│   ├── gmail_watcher/
│   │   ├── imap.py
│   │   ├── classifier.py
│   │   └── state_writer.py
│   ├── application/
│   │   ├── resume_tailor.py
│   │   ├── cover_letter.py
│   │   └── sender.py
│   ├── workers/                # container entrypoints
│   │   ├── scheduler.py
│   │   ├── crawler.py
│   │   ├── extractor_worker.py
│   │   ├── ranker_worker.py
│   │   ├── notifier_worker.py
│   │   ├── gmail_worker.py
│   │   └── identity_warmup.py
│   ├── api/                    # FastAPI + /metrics + admin
│   │   ├── main.py
│   │   ├── metrics.py
│   │   ├── health.py
│   │   └── admin.py
│   └── cli/                    # admin CLI (click)
│       ├── main.py
│       ├── sources.py
│       ├── identity.py
│       └── opps.py
│
├── scripts/                    # shell, not Python
│   ├── bootstrap.sh
│   ├── backup.sh               # pg_dump | age encrypt | rclone
│   ├── restore_drill.sh
│   └── pretest_cf.sh
│
├── tests/                      # mirrors src/ layout
│
├── docs/
│   ├── specs/
│   ├── runbooks/
│   │   ├── pi_recovery.md
│   │   ├── source_quarantine.md
│   │   └── identity_ban.md
│   └── adrs/
│
└── grafana/
    └── dashboards/
        └── agent_jobs.json
```

### Structural rules

- One file = one concern. Split when a file exceeds 300 lines.
- Every subsystem ships a `base.py` (Protocol interface, swappable implementations).
- **No cross-subsystem imports.** Subsystems communicate via Redis Streams only.
- `common/` is the only universal import target.
- Config is data: YAML/JSON in `config/`, prompts in `config/prompts/*.txt`. Never inline.
- Tests mirror `src/` structure.
- Browser tier lives in `src/fetchers/browser/` (5 files), built into separate Docker image (`docker/camoufox.Dockerfile`) — Firefox + Xvfb adds ~400MB to base image. Communicates with rest of system via Redis Streams only.

---

## Tech stack

- **Language**: Python, `uv`-managed.
- **Deps** (`uv add ...`): `discord.py`, `curl_cffi`, `httpx`, `asyncpg`, `redis`, `fastapi`, `prometheus-client`, `sentence-transformers`, `pydantic`, `apscheduler`, `aiosmtplib`, `aioimaplib`, `openrouter`, `pynacl`, `sops-config`.
- **DB**: Postgres 16 + pgvector.
- **Queue**: Redis 7 Streams (AOF `everysec`).
- **Containers**: Docker Compose, ARM64 images only.
- **Browser**: camoufox 0.4+ (Firefox-based) + Xvfb + `ghost-cursor-python`.
- **CF cookie broker**: FlareSolverr (cookie issuance only — not scraper).
- **Embeddings**: sentence-transformers MiniLM (~250MB resident).
- **LLM**: OpenRouter (Gemini Flash for tier-2 extraction, JSON-schema-validated).
- **Outbound mail**: Resend.
- **Inbound**: Gmail IMAP IDLE.
- **Secrets**: SOPS + age, encrypted YAML in git.
- **Identity vault**: libsodium `crypto_secretbox` per-row in Postgres, master key in SOPS.
- **Backups**: `pg_dump | age encrypt | rclone → R2`.

---

## Container / service map

```
Pi 5 Docker Compose stack
├── postgres:16-alpine (pgvector)         # WAL archive → /mnt/storage/wal_archive/
├── redis:7-alpine (AOF everysec)
├── flaresolverr/flaresolverr:latest      # cookie broker only
├── camoufox-worker (3 replicas, mem_limit 1G, tmpfs cache)
├── jobs-scheduler (apscheduler)
├── crawler-workers (3 replicas)
├── extractor-worker
├── ranker-worker
├── notifier-discord (discord.py gateway)
├── gmail-watcher (IMAP IDLE)
├── api-service (FastAPI + /metrics + admin)
└── obsidian-writer (mounts vault dir)

Untouched on host:
- Jellyfin, Prometheus, Grafana, Cloudflared, Tailscale, PiVPN

Future (Phase 4+):
+ piclaw-bot (separate container after Pi 3 → Pi 5 migration)
```

Reserved ports (Docker-internal only, no host publish): 5432, 6379, 9090, 8191.

---

## Data model (Phase 1 core schema, condensed)

```sql
users(id PK, handle UNIQUE, display_name, timezone, status, tier, created_at)

identities(id PK, user_id FK, platform, account_label, encrypted_credentials BYTEA,
           encrypted_cookies BYTEA, cookie_nonce, cred_nonce, fingerprint_id FK,
           proxy_sticky_session_id, email_alias, last_used_at, ban_status,
           warmup_score, warmup_completed)

user_identities(user_id FK, identity_id FK, role ENUM(owner|borrower), granted_at,
                PK(user_id, identity_id), UNIQUE(identity_id) WHERE role='owner')

identity_checkouts(id, identity_id, worker_id, leased_at, expires_at, returned_at)
identity_audit(id, identity_id, action, actor, occurred_at, metadata JSONB)

fingerprints(id, ua_string, viewport, timezone, locale, webgl_hash, canvas_hash,
             font_set_hash, last_assigned_at)

sources(id PK, category, base_url, crawler_strategy, fetch_freq_minutes, priority,
        robots_respected, ban_observed_at, auth_account_id, ranking_weight,
        created_via, discovery_candidate_id, discovery_confidence, status,
        last_successful_crawl_at, opps_extracted_30d, requires_residential,
        browser_mode_required, tier_chain INT[], cf_protection_level,
        last_cf_challenge_at, daily_cost_budget_cents, notes)

opportunities(id UUID PK, source_id FK, canonical_url UNIQUE, title, company,
              description, comp_min, comp_max, comp_currency, location, remote_type,
              category, posted_at, expires_at, apply_url,
              apply_method ENUM(email|ats_form|external|in_platform|embedded_form),
              raw_payload_s3_key, fingerprint_hash, embedding VECTOR(384),
              state ENUM(...), first_seen, last_seen, extraction_tier SMALLINT,
              extraction_confidence REAL)

opportunity_scores(user_id FK, opportunity_id FK, score REAL,
                   score_components JSONB, scored_at, ranker_version,
                   PK(user_id, opportunity_id))

opportunity_transitions(id, opportunity_id FK, from_state, to_state, trigger,
                        occurred_at, metadata JSONB)

profiles(id PK, user_id FK NOT NULL DEFAULT 1, embedding VECTOR(384), headline,
         skills TEXT[], target_lanes TEXT[], min_comp_usd_hr NUMERIC, geo_pref,
         updated_at)

applications(id PK, user_id FK, opportunity_id FK, sent_at, method,
             resume_variant_id FK, cover_letter_id FK, response_status, response_at)

notification_routes(user_id FK, channel ENUM, target, enabled, quiet_hours INT4RANGE,
                    discord_channel_id BIGINT, discord_thread_id BIGINT,
                    embed_color INT, route_type ENUM, PK(user_id, channel))

cf_clearance_cache(source_id FK, identity_id FK, domain, cookie_value, ua_string,
                   ja4_profile, ip_solved_from, acquired_at, expires_at,
                   last_used_at, success_count, failure_count,
                   PRIMARY KEY(source_id, identity_id, domain))

usage_ledger(id PK, user_id FK, ts, kind ENUM, provider, model,
             input_tokens, output_tokens, cost_usd_micros BIGINT, correlation_id)
daily_spend(date, source_id, tier, request_count, cents_spent)
```

**v2 reserved names** (locked in `V002`, populated Phase 3+):

```
candidate_sources, discovery_strategies, source_provenance,
resume_variants, target_companies, contacts, outreach_log
```

---

## Critical path (sequential, ~2 days)

All parallel work is blocked until CP is done.

| Step | Owner | Effort | Depends |
|---|---|---|---|
| **CP1** Repo + Docker stack | Claude | 0.5d | E1–E6 (Pi prep) |
| **CP2** Schema migrations V001 + V002 | Claude | 0.5d | CP1 |
| **CP3** SOPS bootstrap | Claude | 0.5d | B1–B7 (signups) |
| **CP4** Sources seed (V003 from `config/sources/*.yaml`) | Claude | 0.5d | CP2 |

**Day 1 = CP1. Day 2 = CP2 + CP3 + CP4 in sequence.**

---

## Parallel build tracks (Day 3+, after CP)

⚪ = parallel-eligible with other ⚪ tracks.

| Track | Scope | Effort | Key files |
|---|---|---|---|
| 1 | HTTP fetcher + cookie cache | 1.5d | `fetchers/http.py`, `fetchers/flaresolverr.py`, `fetchers/dispatcher.py`, `V005__cf_clearance_cache_indexes.sql` |
| 2 | ATS API sources (Greenhouse/Lever/Ashby/Workable) | 1d | `sources/ats/*.py`, `config/sources/*_slugs.yaml` |
| 3 | RSS + GitHub + HN + Reddit | 0.5d | `sources/rss/*`, `sources/github_markdown/*`, `sources/hn_algolia.py`, `sources/reddit_forhire.py` |
| 4 | Browser tier (camoufox + Xvfb + pool) | 1.5d | `fetchers/browser/*`, `docker/camoufox.Dockerfile` |
| 5 | Auth-gated scrapers (Internshala, Cuvette, Unstop, Contra) | 1d | `sources/india/*`, `sources/freelance/contra.py` |
| 6 | Extractor cascade (T0 regex → T1 selectors → T2 LLM) | 1.5d | `extractors/*` |
| 7 | Profile + ranker | 1d | `ranker/*` |
| 8 | Discord notifier (23 slash commands, embeds, buttons, reactions, modals) | 1.5d | `notifiers/discord/*` |
| 9 | Apply + outcome (resume tailor, cover letter, Resend, audit log) | 1.5d | `application/*`, `notifiers/discord/handlers/modals.py` |
| 10 | Gmail watcher + classifier | 1d | `gmail_watcher/*` |
| 11 | Freelance speed lane (Contra hot, r/forhire push, Upwork email, Telegram) | 1d | `sources/freelance/*`, `notifiers/discord/embeds/priority_push.py` |
| 12 | Fellowship + India + founder signal | 0.5d | `sources/fellowship/*`, `sources/india/{yc_india,inc42,yourstory}.py` |
| 13 | Security hardening | 0.5d | `scripts/bootstrap.sh`, `.pre-commit-config.yaml` |
| 14 | Observability (Prometheus exporter + Grafana dashboard + restore drill) | 0.5d | `api/metrics.py`, `grafana/dashboards/agent_jobs.json`, `scripts/restore_drill.sh` |

**Track 8 has zero dependency on scrapers** — build with mocked data, integrate Day 7+.

### Blocking dependencies

| Task | Blocks |
|---|---|
| E1–E6 (Pi prep) | CP1 |
| B1 (OpenRouter) | Track 6.3 + Track 9 + Track 11.3 |
| B2 (Resend) | Track 9.3 |
| B3 (Gmail OAuth) | Track 10 |
| B4 (Discord bot) | Track 8 |
| B5 (Telegram api) | Track 11.6 |
| B6 (Reddit) | Track 3.5 |
| B7 (R2) | Track 13.6 |
| C1–C2 (email aliases) | Tracks 5.1–5.4 + 11.5 |
| F1–F5 (Discord channels) | Track 8 |
| A1–A6 (profile assets) | Track 7 |
| CP1–CP4 | everything after |
| Track 1 | Tracks 5 + 6 |
| Track 6 | Track 7 |
| Tracks 7 + 8 | first digest delivery |
| Track 9 | Track 11 |

---

## Compressed calendar (best 10 days, realistic 12)

```
DAY -3 to 0  → [U] Tracks A,B,C,D,E,F (~6–8h spread)
DAY 1        → [C] CP1 (Docker stack)
                [U] continue warmup, finish profile assets
DAY 2        → [C] CP2 (schemas) + CP3 (SOPS) + CP4 (sources seed)
═══════════════ CP DONE — PARALLEL EXPLOSION ═══════════════
DAY 3        → [C] T1 ║ T2 ║ T3 ║ T8 scaffold
DAY 4        → [C] T1 cont ║ T4 ║ T8 cont ║ T13
DAY 5        → [C] T4 cont ║ T5 ║ T6 ║ T10
DAY 6        → [C] T5 cont ║ T6 cont ║ T12 ║ T8 integration
DAY 7        → [C] T7 ║ T12 cont ║ first end-to-end happy-path
                [U] hand-rate 30 opps for ranker calibration
DAY 8        → [C] T9 ║ T10 finalize ║ ranker weights refit
DAY 9        → [C] T11 ║ T14
DAY 10       → [C] integration + bug squash + restore drill ║ GO LIVE
                [U] first 5 applications fired
```

---

## CF evasion stack (Phase 1, locked)

```
T0 — curl_cffi 0.7+ impersonate=chrome131 + cached cf_clearance
T1 — FlareSolverr Docker (cookie broker only, NOT scraper)
T2 — camoufox 0.4+ (Firefox-based, ARM64) + Xvfb + ghost-cursor-python
T3 — CDP sidecar (DEFERRED to Phase 4 if needed)
T4 — ZenRows premium ($7.49/1000, cost-gated)
T5 — Bright Data Scraping Browser (manual whitelist only)
```

### Per-target route-around

- **Wellfound** → ATS slug harvest (T0). Skip CF entirely.
- **Cuvette** → mobile API, iOS UA (T0).
- **Unstop** → public JSON API + sitemap (T0).
- **Hirect** → DROPPED. Founder-signal substitution via YC + Twitter (v2).
- **Naukri** (v2) → `jobapi/v3/search` JSON (T0).
- **Upwork** (v2) → email digest pipeline (T0 IMAP).

---

## Slash commands (23)

```
/budget set <min>          /budget today <min>        /budget status
/digest now                /digest preview            /digest schedule <hhmm>
/apply <opp_id>            /skip <opp_id>             /snooze <opp_id> <days>
/pin <opp_id>              /status
/source list               /source pause <name>       /source resume <name>
/source add <url> <lane>
/identity status           /identity update <field> <val>
/cost today                /cost cap <usd>
/followup <opp_id>         /explain <opp_id>          /export <range>
/review                    # v2 dark-source candidates (Phase 3)
```

---

## Discord server layout

```
📥 AGENT — DIGEST
  #📰-daily-digest         (text)
  #⚡-priority-push         (text)

🗂️ AGENT — OPPS
  #💼-fulltime             (forum)
  #🎓-internships          (forum)
  #🏆-fellowships          (forum)
  #💸-freelance            (forum)

📋 AGENT — TRACKER
  #✅-applied              (forum)
  #📬-responses            (text)
  #🎤-interviews           (forum)
  #🎯-offers               (text)

⚙️ AGENT — SYSTEM
  #🔔-alerts               (text)
  #💰-costs                (text)
  #🛠-source-health        (text)
  #🤖-bot-logs             (text, muted)
```

Bot perms scoped to JOBS category only: `Manage Threads`, `Send Messages`, `Create Public Threads`, `Send Messages in Threads`, `Embed Links`, `Add Reactions`, `Use Application Commands`.

### Bot identity decisions

| Question | Decision |
|---|---|
| Single bot or separate? | Separate jobs bot (NEW Discord application) — **NOT** a PiClaw cog |
| Bot display name | **Hop** (Grace Hopper — built the first compiler, 1952; coined "debugging" after the moth). Double meaning: the bot literally hops between 28+ sources daily. Sibling to user's existing `Ada` personal assistant — both named for women who founded computing. |
| Repo / codename | `marked-path` (internal); system metaphor = `Cartograph` (used in docs/microcopy) |
| Server | Same existing personal server (Hop = 2nd bot member alongside Ada) |
| Perm scope | JOBS category only |
| Slash command prefix | None — own bot owns own commands |
| PiClaw coexists | Yes (Pi 3 currently; migrate to Pi 5 in Phase 4) |

---

## Prometheus metrics

```
# Pipeline
fetch_latency_seconds{source, tier}
fetch_errors_total{class}
extract_selector_miss_total{source}
extract_tier_distribution{source, tier}
dedup_hits_total{lane}
score_latency_seconds
llm_refusals_total
llm_cost_usd_total{kind, model}
digest_size
digest_attention_minutes
deliver_success_total{channel}
applications_sent_total{method}
outcome_events_total{type}

# CF (7 critical signals)
cf_clearance_solve_rate
cf_challenge_appeared_rate
cf_js_challenge_solve_time_ms          # histogram
cf_403_with_ray_header_per_hour
cf_attention_required_body_per_hour
cf_checking_browser_persistent_per_hour
cf_bm_cookie_rotation_rate

# Infrastructure
node_filesystem_avail
postgres_connections
redis_stream_length{stream}
identity_checkout_active_count
identity_ban_status_count{status}
```

---

## Security primitives

| Item | Spec |
|---|---|
| Infra secrets | SOPS+age encrypted YAML in git; decrypted at compose-up |
| Identity vault | libsodium `crypto_secretbox` per-row in Postgres; master key in SOPS |
| Pi access | SSH keys only, bound to Tailscale interface, fail2ban 3-attempt |
| Cloudflared | Public ingress = `/webhooks/*` only; admin/Grafana = Tailscale only |
| Postgres | Docker network only; no host port published |
| Redis | `requirepass` + `rename-command FLUSHALL ""` + Docker network |
| Backups | `pg_dump \| age encrypt \| rclone → R2` (double-encrypted) |
| Pre-commit | Gitleaks + GitHub push protection |
| Cost alerts | OpenRouter daily cap, Resend monthly cap, R2 egress 80% trigger |
| LLM sandboxing | No tool access on extractor LLM; delimiter fencing (`<IGNORE>...</IGNORE>`); JSON schema validated |
| Discord bot perms | JOBS category only |
| Restore drill | Weekly into tmpfs |

---

## Cost model

| Phase | Monthly |
|---|---|
| Phase 0 | $0 (signups only) |
| Phase 1 MVP | $5–15 (OpenRouter + R2) |
| Phase 2 conversion | +$5–10 |
| Phase 3 multi-channel | +$10–25 (Twitter API basic if needed = $100, defer) |
| Phase 4 multi-user + proxies | +$50–100 (residential proxies) |
| Phase 5 sidecar + NVMe | +$60 one-time hardware |

**Hard caps**: $3/day default, $10/day kill switch, $30/mo soft warning, $100/mo hard warning.

---

## LaTeX resume subsystem (Phase 1 — design ratified by 4-specialist review)

Replaces the JSON `config/profile/resume.json` template with the user's actual AltaCV LaTeX resume tree at `config/profile/my_resume/`. The pipeline parses LaTeX → tailors selected bullets via LLM → splices back into a copy of the user's files → compiles to PDF via `tectonic` → attaches the PDF to outgoing applications.

### Why

- LaTeX is the user's existing source of truth (Overleaf).
- AltaCV is highly structured (`\cvevent`, `\cvproject`, `\cvsection`) → parser can auto-detect tailorable blocks with **zero markup** from the user.
- Tailored PDFs match the user's visual identity verbatim — recruiters see the resume they expected, just with bullets emphasizing the role keywords.

### Files

```
config/profile/my_resume/
├── altacv.cls               # styling — never touched
├── mmayer.tex               # main file with \cvsection + \cvevent + \cvproject blocks
├── page1sidebar.tex         # education sidebar
├── sample.bib               # empty, kept to satisfy \addbibresource
└── manifest.yaml            # NEW — main_file, class_file, macro vocabulary, exclude_sections, output_name
```

### New package `src/application/resume_latex/`

| File | Job |
|---|---|
| `parser/manifest.py` | Pydantic load + validate `manifest.yaml` |
| `parser/lexer.py` | `pylatexenc` walker → token stream |
| `parser/blocks.py` | Match macro vocabulary → `Document(blocks=[Block(id, kind, title, bullets, file, char_range)], files, source_hashes)` |
| `selector.py` | Rank blocks vs opp by keyword vote (moved out of `sender.py`) |
| `sanitizer.py` | LaTeX-escape LLM output (allowlist) + macro-denylist (`\write18`, `\input`, `\openin`, `\openout`, `\read`, `\catcode`, `\immediate`, `\directlua`, `\loop`, `\csname`, `\def`, `\xdef`, `\let`, `\expandafter`) |
| `render.py` | Splice edits (descending offset order) → atomic write to `/var/lib/agent/resume_artifacts/<user_id>/<opp_id>.partial/` → rename `.complete/` on success |
| `compile.py` | `subprocess.run(['tectonic','-X','compile','--untrusted', ...], timeout=30, kill_group)`; on exit 0 → `qpdf --linearize` + `exiftool -all:all=` metadata strip; returns `CompileResult(pdf_path, log_path, duration_ms, tectonic_version)` |
| `plaintext.py` | `pylatexenc` → plain text for profile embedding (replaces `resume.json` for ranker) |
| `fallback.py` | Pre-compiled untailored PDF at boot, cached on disk. Used if tailoring or compile fails. Re-warmed by inotify on `config/profile/my_resume/` |

### Apply-flow change

```
user clicks Apply → applier-worker consumes Streams.APPLY → sender.send_application(opp_id):
  1. doc = parser.parse(manifest)                              # boot-cached + inotify watched
  2. blocks = selector.rank(doc.blocks, opp, variant)[:3]
  3. raw_bullets = await llm.tailor(blocks, opp, variant)      # cost-gated via common/llm.py
  4. safe_bullets = sanitizer.escape_and_check(raw_bullets)    # rejects forbidden macros
  5. tree_dir = render.write_partial(doc, edits, /var/lib/agent/resume_artifacts/<user_id>/<opp_id>)
  6. result = compile.run(tree_dir / manifest.main_file)        # 30s timeout, --untrusted, no-net
  7. on success: rename .partial → .complete; insert applications row with
       resume_artifact_sha256, resume_source_hash, resume_compile_status='tailored',
       payload->>'resume_pdf_path'=<final pdf path>
     on fail:   resume_compile_status='fallback' + fallback.get(variant) → attach untailored PDF
  8. attach PDF to Resend email (PDF NEVER posted to Discord channel — DM only or skip)
  9. emit metrics + structured log
```

### Container layout

- **New** `docker/applier.Dockerfile` extends `jobs-bot.Dockerfile`. Adds `tectonic`, `qpdf`, `exiftool`, `pylatexenc`. Only the `applier-worker` service uses this image. Base image stays lean for other 8 workers.
- **New** named volume `tectonic_cache` mounted at `/var/lib/tectonic` (env `XDG_CACHE_HOME=/var/lib/tectonic`). Survives image rebuild.
- TeX bundle pre-warmed at image build via `RUN tectonic --only-cached-fonts /opt/warmup.tex` → cold-compile ~30s → ~2s.
- `applier-worker` runs with `cap_drop: [ALL]`, `read_only: true` rootfs (only `/var/lib/agent/resume_artifacts/<user_id>/` RW), `mem_limit: 512m`, `pids_limit: 64`, `user: 1000`. Defense-in-depth alongside subprocess timeout + tectonic `--untrusted`.

### Migration `V007__resume_artifacts.sql`

```sql
ALTER TABLE applications
  ADD COLUMN resume_artifact_sha256 CHAR(64),
  ADD COLUMN resume_source_hash    CHAR(64),
  ADD COLUMN resume_compile_status TEXT CHECK (resume_compile_status IN ('tailored','fallback','failed'));

CREATE TABLE resume_compile_log (
  id                  BIGSERIAL PRIMARY KEY,
  opportunity_id      UUID REFERENCES opportunities(id) ON DELETE CASCADE,
  user_id             BIGINT NOT NULL DEFAULT 1 REFERENCES users(id),
  source_hash         CHAR(64),
  artifact_sha256     CHAR(64),
  block_overrides     JSONB,
  compile_duration_ms INT,
  tectonic_version    TEXT,
  status              TEXT,
  tectonic_stderr     TEXT,
  created_at          TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX ON resume_compile_log (opportunity_id);

ALTER TABLE resume_variants ADD COLUMN source_kind TEXT
  CHECK (source_kind IN ('json','latex')) DEFAULT 'latex';
```

### New Prometheus metrics

```
resume_compile_duration_seconds{outcome, cold_cache}      # histogram
resume_compile_failures_total{class}                       # tectonic|timeout|sanitizer|network
resume_fallback_used_total
resume_source_hash_drift_total                             # parse-vs-render mismatch
resume_cache_size_bytes
resume_cache_hit_ratio
pdf_artifact_disk_bytes
tectonic_package_fetch_seconds                             # histogram
```

Alerts: p95 compile > 10s for 10min → degraded; failure rate > 0.2/min → page; cache > 280MB → prune trigger.

### Hard rules — non-negotiable

1. **Never splice raw LLM output.** `sanitizer.escape_and_check` runs between LLM and `render`. Reject any LLM bullet containing a `\command` outside the allowlist (`\textbf`, `\textit`, `\emph`, escaped specials).
2. **`tectonic --untrusted` always.** Disables `\write18`, restricts file reads to working dir. Plus subprocess `timeout=30s` and `kill_group=True`.
3. **Artifact dir on disk, never tmpfs.** `/var/lib/agent/resume_artifacts/<user_id>/<opp_id>.partial/` → atomic rename to `.complete/`. Boot-time sweep deletes `.partial/`.
4. **PDF metadata scrubbed.** `\hypersetup{pdftitle=Resume, pdfauthor=..., pdfcreator={}, pdfproducer={}}` in `manifest.yaml`-driven post-edit step + `exiftool -all:all=` post-compile.
5. **PDF NEVER posted to a Discord channel.** Email attachment only. Discord posts a link/summary, not the file. (Discord CDN URLs are crawlable.)
6. **`profile.jpg` EXIF stripped before commit.** Pillow re-encode or `exiftool -all=`.
7. **Source-hash drift guard.** `Document.source_hashes` recorded at parse; `render()` re-reads + verifies — raises `SourceDriftError` on mismatch.
8. **Macro vocabulary in `manifest.yaml`**, not hardcoded. Future class swap (moderncv, Awesome-CV) = config edit.
9. **`user_id` on day one.** Resume tree path, `manifest.yaml`, artifact dir, `resume_compile_log.user_id` — all carry `user_id NOT NULL DEFAULT 1`. Phase 4 multi-tenant requires zero retrofits.
10. **`MP_RESUME_LATEX_ENABLED` feature flag.** Staged rollout: ship code with flag off → backfill embeddings → drain Streams.APPLY → flip flag → 7d clean → remove JSON branches in separate PR.

### Deferred to Phase 2+

- Dedicated `resume-compile` sidecar with `network_mode: none` (current volume = 5 applies/day → applier-worker colocation fine; revisit if concurrency grows)
- Tailscale-signed-URL PDF distribution via Discord DM
- Per-tenant `age` encryption of artifacts at rest (Phase 4)
- moderncv / Awesome-CV preset manifests
- CI malicious `.tex` corpus regression harness
- `inotify` watch on `config/profile/my_resume/` for hot-reload (Phase 1 = re-parse on every applier startup)

### Specialist review verdicts

| Lens | Verdict | Top blocker addressed |
|---|---|---|
| Backend Architect | APPROVE_WITH_AMENDMENTS | Compile outside notifier loop; durable artifact dir; source-hash drift; V007 schema |
| Security Engineer | APPROVE_WITH_AMENDMENTS | LaTeX injection via LLM (sanitizer + macro denylist); tectonic sandbox; PDF metadata; no Discord PDF |
| DevOps / SRE | APPROVE_WITH_AMENDMENTS | Separate applier.Dockerfile; tectonic_cache volume; Jellyfin-aware throttle reuse; atomic state machine |
| Senior Engineer | APPROVE_WITH_AMENDMENTS | char_range descending splice; block_id = sha; CompileError types; feature flag rollout |

Full review records: ephemeral specialist agents; key amendments folded above.

---

## Failure domains + recovery

| Failure | Detection | RTO | RPO |
|---|---|---|---|
| Pi crash / power loss | Compose `restart: unless-stopped` + Prometheus alert | <5min auto | ≤5min (WAL archive every 5min) |
| Internal SD card fails | SMART + `pg_amcheck` weekly + dmesg | 2–4h (reflash + restore) | ≤5min |
| Network drop | Outbound DNS probe | Auto on reconnect | 0 |
| Source ban | `source_health` rolling 24h | Auto-quarantine + alert | N/A |
| OpenRouter outage | 5xx rate >50% | Circuit breaker → embeddings-only | 0 (deferred resumes) |
| Postgres corruption | `pg_amcheck` weekly | 1–2h (PITR from base + WAL) | ≤5min |
| Discord gateway disconnect | `READY` event absence >5min | Auto-reconnect with backoff | 0 |
| Disk >85% full | Prometheus alert | <30min auto-prune | 0 |
| LLM cost cap reached | `daily_spend` trigger | Auto-defer to next day | N/A |
| Identity ban cascade | `ban_status` flip | Auto-quarantine siblings + alert | N/A |
| Tectonic compile fail (sanitizer reject, timeout, package fetch) | `resume_compile_failures_total` rate >0.2/min | Auto-fallback to untailored PDF + alert | 0 (next apply retries tailoring) |
| Resume source drift mid-render | `SourceDriftError` raised | Re-parse + retry once; on second fail → fallback PDF | 0 |
| Tectonic cache corruption | First-compile latency >60s | `rm -rf tectonic_cache && restart applier-worker` | 0 (cache rebuildable from network) |

---

## Phased roadmap

### Phase 1 — MVP (Days 1–10)

Covered above. Ships the daily digest, apply/skip flow, Gmail outcome tracking, freelance speed lane.

### Phase 2 — Conversion v1.1 (7–14 days post-MVP)

- Cold email outbound lane (Apollo/Hunter, max 10/day/identity, warmup ramp).
- Resume A/B variants with `application.resume_variant_id` tracking.
- Follow-up automation (13:00 cron, LLM draft, button-driven send).
- Source response-rate feedback (logistic regression refit weekly).

### Phase 3 — Multi-channel v1.2 (Weeks 5–8)

- Twitter/X founder signal scraper (Nitter or paid API).
- Dark-source discovery worker (Google dorking + Reddit + HN + GitHub awesome-lists + Common Crawl + newsletters).
- Bounty lane (Algora, Replit Bounties, Gitcoin).
- OSS contribution funnel (`target_companies` + "good first issue" scan).

### Phase 4 — Multi-user v2.0 (Weeks 8–10)

- Identity vault hardening (libsodium per-row encryption).
- Multi-tenant cutover (drop `user_id DEFAULT 1`, add tenant resolver middleware, `/jobs-onboard <token>`).
- Residential proxy pool (only if home ISP banned — Smartproxy / IPRoyal).
- CDP sidecar (only if T2 camoufox fails sustained — used ThinkCentre M720q ~₹12–15K).
- PiClaw migration Pi 3 → Pi 5 (rsync vault, redeploy container).

### Phase 5 — Polish v2.1+

- NVMe HAT + SSD migration (~$60, removes SD card durability concern).
- Web dashboard (Next.js + PostgREST views over Postgres).
- Advanced ranker (response-rate-weighted ML refit nightly).
- Cost optimization (local Llama for cheap LLM tasks).
- Multi-region cloud VPS backup (if Pi reliability insufficient).

---

## Defer list (Phase 1 — DO NOT BUILD)

- Upwork direct integration (use email digest only).
- CDP sidecar mini-PC.
- Multi-user / friend Internshala.
- Residential proxies.
- Cold email automation.
- Warm intro mining.
- Resume A/B testing.
- Twitter signal scraping.
- Dark-source discovery worker.
- OSS contribution funnel.
- Web dashboard.
- Bounty platforms.
- LUKS encryption (Pi physically safe per user).
- NVMe HAT.
- Sidecar Chrome.
- Per-lane forum channel variations.

**Hard rule**: ONE feature per week MAX after Day 14. Until first ₹X earned, only features that improve apply rate or response rate are allowed.

---

## Success metrics

| Phase | Metric | Target |
|---|---|---|
| Phase 1 | Days to first apply | < 14 |
| Phase 1 | Daily digest delivery success | > 95% |
| Phase 1 | Per-source extraction success | > 80% |
| Phase 1 | CF clearance solve rate | > 70% |
| Phase 2 | Response rate (any) | > 5% (industry avg 1–3%) |
| Phase 2 | Apply rate sustained | > 5/day |
| Phase 3 | New sources discovered/week | > 3 |
| Phase 4 | Multi-user end-to-end | working with `user_id=2` |
| All | Monthly cost | < $30 until earning |

---

## Verification (Day 14 go-live checklist)

1. **Postgres durability**: kill power during write load. On boot: `pg_amcheck` clean, last 5min of opps may be lost, DB consistent.
2. **Redis durability**: same test. AOF replay, max 1s data loss.
3. **Restore drill**: `bash scripts/restore_drill.sh` restores latest `pg_dump` from R2 into tmpfs Postgres; schema + row counts match prod.
4. **CF clearance**: hit a CF-protected source 10 times. `cf_clearance_solve_rate` > 70%. Inspect `cf_clearance_cache` for reuse.
5. **End-to-end happy path**: manual fetch of a Greenhouse source → opp appears in `opportunities` within 60s → `opportunity_scores` row written → embed posted to `#📰-daily-digest` → click Apply → `applications` row + forum thread in `#✅-applied`.
6. **Reaction handler**: ✅ on an opp embed mutates state identically to button click.
7. **Slash commands**: `/status` returns pipeline overview; `/budget today 30` updates DB; `/source list` returns rows.
8. **Gmail watcher**: send test auto-rejection email to monitored inbox → opp state transitions to `rejected` + message posted in tracker thread.
9. **Behavioral nudge**: at 9pm, if `applications_sent_today < target`, @mention fires in `#🔔-alerts`.
10. **Prometheus**: existing Prometheus scrapes `:9090/metrics`; all listed metrics present. Grafana panels render.
11. **Cost cap**: artificially inflate `daily_spend` past $3 → next LLM call refuses + alert fires.
12. **Identity isolation**: `UPDATE identities SET ban_status='banned' WHERE id=X` → sibling identities with same `fingerprint_id` auto-quarantined within 5min.

If 12/12 pass: pipeline is production-ready for solo use. First 5 manual applies fired same day.

---

## Open items (user action)

| # | Item | Blocking |
|---|---|---|
| 1 | ~~Pick jobs bot name~~ **Locked: Hop** (Grace Hopper) | Done |
| 2 | OpenRouter API key | Phase 1 Day 2 |
| 3 | New Discord application + bot + invite | Phase 0 |
| 4 | Cloudflare Email Routing catch-all + aliases | Phase 0 |
| 5 | Master resume → JSON | Phase 0 |
| 6 | Skill matrix YAML | Phase 0 |
| 7 | Comp floors (₹X intern, ₹Y FT, $Z/hr freelance) | Phase 0 |
| 8 | Identity warmup on platforms | Phase 0, start Day -3 |
| 9 | Resend signup + verify sender domain | Phase 0 |
| 10 | Google Cloud project + Gmail OAuth | Phase 0 (or before Day 12) |
| 11 | Telegram `api_id` / `api_hash` | Before Day 13 |
| 12 | Worker Gmail (`upwork-worker@yourdomain.tld`) | Before Day 13 |

---

## Working conventions for Claude

- **Always** `cd /home/lakshit_gupta/coding/Marked_Path/` at the start of a build session.
- **Never** add an `agent-jobs/` (or any other) wrapper folder. Root is flat.
- **Never** set `synchronous_commit=off` or disable `full_page_writes` on Postgres. Power-fail risk is real (no UPS).
- **Never** publish Postgres or Redis ports to the host. Docker network only.
- **Never** import across subsystems. Talk via Redis Streams. `common/` is the only universal import.
- **Never** inline prompts or config in code. Prompts live in `config/prompts/*.txt`, config in `config/*.yaml`.
- Split any file that exceeds ~300 lines.
- Every subsystem has a `base.py` Protocol — swap implementations without touching callers.
- Browser tier is a separate Docker image (`docker/camoufox.Dockerfile`) because Firefox + Xvfb adds ~400MB.
- Tier-2 LLM extractor: no tool access, delimiter-fenced input (`<IGNORE>...</IGNORE>`), JSON-schema validated output.
- Cost gate: every LLM call goes through `common/llm.py`. It checks `daily_spend` and refuses past cap.
- Identity vault: never log decrypted credentials. Per-row libsodium boxes, master key in SOPS.
- Discord bot perms: JOBS category only — never grant server-wide.
- **Feature gate after Day 14**: ONE feature per week max. Only features that improve apply rate or response rate are allowed until first earnings.

---

## Pointer back to plan

Full plan with all sub-tasks, tier-chain details, and rationale: `/home/lakshit_gupta/.claude/plans/virtual-splashing-pine.md`.
