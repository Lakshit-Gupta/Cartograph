# Phase 0 + Phase 1 Closeout — Design

**Date:** 2026-05-18
**Status:** Approved (design)
**Scope:** Complete remaining Phase 0 + Phase 1 work and ship Cartograph to production on Pi 5.
**Estimated effort:** 6–9 working days, 8 sequential stages.

## Context

Per audit on 2026-05-18:
- ~9,500 LoC across 115 Python files
- 8 tests across 4 files (essentially zero coverage)
- 5 days have elapsed since project kickoff with Phase 0+1 still incomplete
- Several heavy subsystems already implemented: Discord notifier (23 cmds, 2,258 LoC), Apply flow (671 LoC), Gmail watcher (443 LoC), Extractors (1,200+ LoC), Ranker (253 LoC)
- Several remain stubs: ATS (Lever/Ashby/Workable), RSS, GitHub markdown, browser tier, auth-gated scrapers, fellowships, freelance push
- Three services disabled for local dev via `docker-compose.override.yml`: notifier-discord, gmail-watcher, identity-warmup
- Pre-commit hook never installed locally
- Pytest never invoked during the previous session

The user has provided all credentials in `secrets.yaml` (SOPS-encrypted). Outstanding user-only action: identity warmup on auth-gated platforms.

This design closes the project out as one coordinated plan rather than six loose sub-projects.

## Architecture (unchanged)

The pipeline architecture remains as defined in CLAUDE.md and the long-form plan at `~/.claude/plans/virtual-splashing-pine.md`. No new subsystems are introduced. This closeout fills in the partial implementations, replaces stubs with real fetchers, and brings the Discord + Gmail + apply flows online end-to-end.

## Stages (sequential, gated by acceptance criteria)

### Stage 0 — Unblock + happy-path (today, 3-4 h)

**Objective:** kill all dev-mode gates, deliver first opportunity to Discord, prove no infrastructure surprises.

**Tasks:**
1. `pre-commit install` (one-off, user laptop). Confirms `.pre-commit-config.yaml` hooks now auto-fire on commit.
2. Empty / delete `docker-compose.override.yml`. Re-enables `notifier-discord`, `gmail-watcher`, `identity-warmup`.
3. Audit `secrets.yaml` for any zero / empty field via `sops -d secrets.yaml | grep -E ': "?(0|""|null)"?$'`. Fix anything missing.
4. `docker compose build && docker compose up -d --force-recreate`.
5. Smoke `ats_greenhouse / stripe` slug through pipeline: scheduler tick → crawler → extractor → ranker → notifier.
6. `make test` (existing 8 tests). Fix any red.

**Acceptance:**
- 14 / 14 containers Up; postgres + redis healthy; no Restarting state.
- ≥ 1 row in `opportunities` with `source_id` matching `ats_greenhouse`.
- ≥ 1 row in `opportunity_scores`.
- ≥ 1 Hop embed visible in `#📰-daily-digest`.
- `make test` green.
- 10-min log window clean (no error / critical / fatal).

**Risks already preempted:**
- Pre-commit `migrate-replay` hook → already wired.
- Redis OOM → MAXLEN caps already in place.
- pgvector codec → already registered on pool.

### Stage 1 — ATS + Aggregator stubs (Day 1, 1 d)

**Objective:** replace stub fetchers with real ones. Unlock 8 net-new sources.

**Files to implement:**
- `src/sources/ats/lever.py` — call `api.lever.co/v1/postings/<slug>?mode=json`. Slugs from `config/sources/lever_slugs.yaml`.
- `src/sources/ats/ashby.py` — POST `jobs.ashbyhq.com/api/non-user-graphql` with `op=ApiJobBoardWithTeams`. Slugs from `config/sources/ashby_slugs.yaml`.
- `src/sources/ats/workable.py` — `apply.workable.com/api/v3/accounts/<slug>/jobs`. Slugs from `config/sources/workable_slugs.yaml`.
- `src/sources/rss/remoteok.py` — `remoteok.com/api` (JSON despite name).
- `src/sources/rss/weworkremotely.py` — RSS feed `weworkremotely.com/categories/remote-programming-jobs.rss` via `feedparser`.
- `src/sources/github_markdown/simplifyjobs.py` — `raw.githubusercontent.com/SimplifyJobs/Summer2024-Internships/dev/README.md` + markdown table parser.
- `src/sources/github_markdown/pittcsc.py` — `raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md`.
- `src/sources/github_markdown/ouckah.py` — same pattern.

**Pattern for each:** implement `Source.plan(self, ctx) -> Iterable[FetchTask]`. Tier-1 selector (`src/extractors/tier1_selectors/<name>.py`) parses the JSON / markdown into `Opportunity` records.

**Acceptance:**
- Each new source returns ≥ 1 opp on a single manual fetch.
- Total opportunities in DB ≥ 200.
- No new error classes in logs.

### Stage 2 — Browser tier + auth-gated scrapers (Day 2-3, 1.5 d)

**Objective:** make camoufox actually spawn Firefox, hook up identity vault, fetch the 4 auth-gated sources.

**Prerequisite (user-only):** sock-puppet accounts on Wellfound, Cuvette, Unstop, Contra. Add via `mp identity add --platform <p> --email <e> --password <pw>` (uses libsodium per-row encryption).

**Browser tier files:**
- `src/fetchers/browser/camoufox.py` — instantiate `camoufox.async_api.AsyncCamoufox(headless=False, ...)` with Xvfb display. Real `await page.goto(url)`. Return `FetchResponse`.
- `src/fetchers/browser/behavioral.py` — wrap interactions in `ghost-cursor-python` for human-like motion.
- `src/fetchers/browser/pool.py` — already present per audit; verify lease + release semantics work under concurrency.
- `src/fetchers/browser/lifecycle.py` — kill + restart after 30 pages.

**Auth-gated source files:**
- `src/sources/india/internshala.py` — login flow + `internshala.com/api/internships/`
- `src/sources/india/cuvette.py` — mobile API (`cuvette.tech/api/v1/student/jobs`), iOS UA per CLAUDE.md route-around.
- `src/sources/india/unstop.py` — public JSON `unstop.com/api/public/opportunity/search` (no auth required, T0 fetch).
- `src/sources/freelance/contra.py` — login flow + `contra.com/api/independents/opportunities/`.

**Identity dispatch:** `src/fetchers/dispatcher.py` already routes tier chains — verify identity-bound requests grab the right vault row + return on completion.

**Acceptance:**
- All 4 auth-gated sources return non-empty opp lists.
- `identity_checkouts` records lease + return events.
- No identity in `banned` or `quarantined` state after 1 h of crawling.
- Total opportunities ≥ 500.

### Stage 3 — Freelance speed lane + Fellowships (Day 4, 1 d)

**Objective:** wire the priority push channel; flesh out 8 fellowship stubs.

**Freelance speed lane:**
- `src/sources/freelance/contra.py` — extend Stage 2 fetcher to push high-comp gigs directly to `Streams.NOTIFY` with `route_type=priority_push`.
- `src/sources/freelance/telegram.py` — Telethon-based listener on configured channels (uses `telegram_api_id`, `telegram_api_hash`).
- `src/sources/freelance/upwork_email.py` — already present per audit; wire into `Streams.NOTIFY`.
- `src/notifiers/discord/embeds/priority_push.py` — already present; verify renders.

**Fellowships (replace 2-line stubs):**
- `src/sources/fellowship/anthropic.py` — scrape `anthropic.com/fellows-program`
- `src/sources/fellowship/cohere_for_ai.py`
- `src/sources/fellowship/huggingface.py`
- `src/sources/fellowship/mats.py`
- `src/sources/fellowship/ml_collective.py`
- `src/sources/fellowship/openai_residency.py`
- `src/sources/fellowship/yc.py` (already partial)
- `src/sources/india/yc_india.py` + `inc42.py` + `yourstory.py` (founder signal)

Each implements `Source.plan` + a tier1 selector. Most are HTML scrape via camoufox (Stage 2 enables this).

**Acceptance:**
- 1 high-comp freelance opp triggers a priority push embed in `#⚡-priority-push`.
- ≥ 5 fellowship opps land in `#🏆-fellowships`.

### Stage 4 — Apply flow / LaTeX resume subsystem (Day 5-6, 1.5 d)

**Objective:** implement the LaTeX resume subsystem per the ratified 4-specialist-review design already locked in CLAUDE.md.

**New container:**
- `docker/applier.Dockerfile` — extends `jobs-bot.Dockerfile`, adds `tectonic`, `qpdf`, `exiftool`, `pylatexenc`. Pre-warms tectonic cache at build via `RUN tectonic --only-cached-fonts /opt/warmup.tex`.
- Named volume `tectonic_cache` mounted at `/var/lib/tectonic` (env `XDG_CACHE_HOME=/var/lib/tectonic`).
- Compose service `applier-worker` runs with `cap_drop: [ALL]`, `read_only: true`, `mem_limit: 512m`, `pids_limit: 64`.

**New package `src/application/resume_latex/`:**
- `parser/manifest.py` — Pydantic loader for `config/profile/my_resume/manifest.yaml`.
- `parser/lexer.py` — `pylatexenc` token stream.
- `parser/blocks.py` — macro-vocabulary block detection → `Document(blocks=[Block(id, kind, title, bullets, file, char_range)], files, source_hashes)`.
- `selector.py` — rank blocks vs opp by keyword vote.
- `sanitizer.py` — LaTeX-escape LLM output (allowlist) + macro denylist.
- `render.py` — splice edits (descending offset) → atomic write to `/var/lib/agent/resume_artifacts/<user_id>/<opp_id>.partial/` → rename `.complete/` on success.
- `compile.py` — `subprocess.run(['tectonic','-X','compile','--untrusted', ...], timeout=30, kill_group=True)` → `qpdf --linearize` + `exiftool -all:all=`.
- `plaintext.py` — `pylatexenc` plain-text for profile embedding.
- `fallback.py` — pre-compiled untailored PDF, re-warmed via inotify on `config/profile/my_resume/`.

**Migration `V007__resume_artifacts.sql`** (already specified in CLAUDE.md):
- `applications` columns: `resume_artifact_sha256`, `resume_source_hash`, `resume_compile_status`.
- New table `resume_compile_log`.
- `resume_variants.source_kind` defaulted to `latex`.

**Feature flag:** `MP_RESUME_LATEX_ENABLED` in `secrets.yaml`. Default off until end-to-end verified, then flip on.

**Apply-flow change in `src/application/sender.py`:**
1. `doc = parser.parse(manifest)` (boot-cached + inotify watched)
2. `blocks = selector.rank(doc.blocks, opp, variant)[:3]`
3. `raw_bullets = await llm.tailor(blocks, opp, variant)` (cost-gated)
4. `safe_bullets = sanitizer.escape_and_check(raw_bullets)`
5. `tree_dir = render.write_partial(doc, edits, ...)`
6. `result = compile.run(tree_dir / manifest.main_file)`
7. On success → rename `.partial → .complete`, insert `applications` row, attach PDF to Resend.
8. On fail → `resume_compile_status='fallback'` + fallback PDF.

**Hard rules per CLAUDE.md (non-negotiable):**
1. Never splice raw LLM output (sanitizer mandatory).
2. `tectonic --untrusted` always, 30 s timeout, `kill_group=True`.
3. Artifact dir on disk, never tmpfs; `.partial → .complete` atomic rename; boot-time sweep deletes `.partial/`.
4. PDF metadata scrubbed via `\hypersetup` + `exiftool -all:all=`.
5. **PDF NEVER posted to Discord channel.** Email attachment only; Discord posts link/summary.
6. `profile.jpg` EXIF stripped before commit.
7. Source-hash drift guard raises `SourceDriftError`.
8. Macro vocabulary in `manifest.yaml`, not hardcoded.
9. `user_id NOT NULL DEFAULT 1` from day one.
10. `MP_RESUME_LATEX_ENABLED` feature flag for staged rollout.

**Acceptance:**
- Click Apply button on a Greenhouse opp embed → tailored PDF compiles in < 10 s → email sent via Resend → `applications` row + `resume_compile_log` row + audit trail in `identity_audit`.
- Tectonic sandbox tests pass (try injecting `\write18` → sanitizer rejects).

### Stage 5 — Gmail watcher live (Day 7, 0.5 d)

**Objective:** outcome tracking via inbound Gmail.

**Files (already present per audit, just enable + wire):**
- `src/gmail_watcher/imap.py` — IMAP IDLE on `gmail_worker_user`.
- `src/gmail_watcher/classifier.py` — LLM classifier via `config/prompts/email_classifier.txt` → `rejection | interview-request | offer | unknown`.
- `src/gmail_watcher/state_writer.py` — `opportunities.state` transition through trigger.
- `src/gmail_watcher/upwork_parser.py` — already present.

**Acceptance:**
- Send a test rejection email to monitored inbox → `opportunity_transitions` row appears within 60 s → embed posted in `#📬-responses`.

### Stage 6 — Pytest coverage + pre-commit (Day 8, 1 d)

**Objective:** defensive quality gate before launch.

**Test scaffold:**
- `tests/fixtures/<source>.json` — saved real API responses per source.
- `tests/extractors/test_tier1_<source>.py` — one test per tier1 selector, validates against fixture.
- `tests/integration/test_pipeline_happy_path.py` — full crawl → extract → rank → notify with mocked HTTP via `respx`.
- `tests/conftest.py` — fixtures for fake redis (`fakeredis`), fake pg pool, mocked LLM.

**Pre-commit additions:**
- `mypy` (strict on `src/common/` only initially).
- `pytest -m smoke` (fast tests only, < 30 s).

**Acceptance:**
- `make test` green.
- `pre-commit run --all-files` green.
- Coverage ≥ 40 % on `src/` (excl. `src/notifiers/discord/embeds/` boilerplate).

### Stage 7 — Observability finalisation (Day 8.5, 0.5 d)

**Objective:** Grafana dashboards rendered, alert rules live.

**Files:**
- `grafana/dashboards/agent_jobs.json` — render panels for: fetch latency by tier, extract tier distribution, CF clearance solve rate, LLM cost USD daily, digest size, applications sent, identity ban_status count, redis stream lengths.
- `grafana/alerts.json` (new) — alert rules:
  - `cf_clearance_solve_rate < 0.5 for 30m` → `#🔔-alerts`
  - `llm_cost_usd_total > daily_cap` → `#🔔-alerts` + kill switch
  - `rate(fetch_errors_total[5m]) > 5/min` → `#🔔-alerts`
  - `redis_memory_used_bytes / redis_maxmemory_bytes > 0.8` → `#🔔-alerts`
- Discord webhook integration in alert manager.

**Acceptance:**
- Set fake cost cap to $0.01, force an LLM call → alert arrives in `#🔔-alerts` within 60 s.

### Stage 8 — Go-live verification (Day 9, 1 d)

Run CLAUDE.md's 12-item verification checklist verbatim. All 12 must pass.

1. Postgres durability under power-cut.
2. Redis durability under power-cut.
3. Restore drill via `scripts/restore_drill.sh`.
4. CF clearance solve rate > 70 %.
5. End-to-end happy path (manual fetch → opp → embed → button click → applications row → forum thread).
6. Reaction handler equivalence (✅ reaction == Apply button).
7. Slash commands sanity (`/status`, `/budget today 30`, `/source list`).
8. Gmail outcome flip (test rejection email → state → tracker post).
9. Behavioral nudge at 21:00 IST if `applications_sent_today < target`.
10. Prometheus scrapes all listed metrics; Grafana panels render.
11. Cost cap: inflate `daily_spend` past $3 → LLM call refuses + alert.
12. Identity ban cascade: `UPDATE identities SET ban_status='banned' WHERE id=X` → siblings auto-quarantined.

**Acceptance:** 12 / 12 pass → fire first 5 manual applies same day.

## Dependencies between stages

```
Stage 0  ──> Stage 1 ──> Stage 3 ────┐
              │                       │
              └──> Stage 2 ───────────┤
                                      ├──> Stage 4 ──> Stage 5 ──> Stage 6 ──> Stage 7 ──> Stage 8
                                      │
                Stage 6 ──────────────┘ (tests can be authored in parallel from Stage 1 onwards)
```

Stages 1 ↔ 2 can be partially parallelised (different code areas). Stage 6 tests can be authored alongside stages 1-5 if context allows. Stage 4 (apply / LaTeX) is the longest single stage.

## User-only Phase 0 action items

| # | Item | Required before stage |
|---|---|---|
| 1 | `pre-commit install` (one command) | Stage 0 |
| 2 | Sock-puppet accounts on Wellfound + Cuvette + Unstop + Contra; passwords added to identity vault via `mp identity add` | Stage 2 |
| 3 | Move to Pi for production deploy (or stay on laptop for dev verification) | Stage 8 |

All other Phase 0 items per CLAUDE.md (OpenRouter, Discord, CF Email Routing, Resend, Gmail OAuth, Telegram, Reddit, R2, profile files) confirmed present in `secrets.yaml` or `config/profile/`.

## Failure modes covered

| Failure | Stage | Mitigation |
|---|---|---|
| pre-commit hook never installed | Stage 0 | First task |
| Notifier crashes on missing channel ID | Stage 0 | `assert_channels_configured` runs at boot |
| Stub source returns empty list and breaks tests | Stage 1 | Real fetch, then test against fixture |
| Camoufox doesn't spawn (X server missing) | Stage 2 | Xvfb in `camoufox.Dockerfile`; test in isolation |
| Identity gets banned mid-Stage 2 | Stage 2 | Ban cascade trigger (already shipped); identity warmup score ≥ threshold |
| Tectonic injection via malicious LLM output | Stage 4 | Sanitizer + macro denylist + `tectonic --untrusted` |
| PDF leaks to Discord | Stage 4 | Hard rule #5: email only, never channel |
| Gmail OAuth refresh-token expires (7-day test mode) | Stage 5 | Publish Google app or rotate manually; CLAUDE.md notes this |
| Test fixtures rot when source API changes | Stage 6 | Quarterly refresh; tier1 selector should fail loud on shape drift |
| Alert spam under partial outage | Stage 7 | `for 30m` minimum window on rate-based alerts |
| Day 8 checklist fails on Pi but works on laptop | Stage 8 | Run on Pi early; ARM64 image parity confirmed via multi-arch `pgvector/pgvector:pg16` |

## Out of scope

- Phase 2+ (cold email, A/B variants, follow-up automation).
- Twitter / X founder signal scraper (Phase 3).
- Dark-source discovery worker (Phase 3).
- Multi-user cutover (Phase 4).
- NVMe HAT (Phase 5).
- Local LLM (Phase 5).

## Spec self-review (inline)

1. **Placeholder scan:** no TBD / TODO / vague language.
2. **Internal consistency:** stage dependencies match the architecture diagram; file lists match CLAUDE.md folder structure.
3. **Scope check:** 8 stages each shippable in 0.5–1.5 days. Each has acceptance criteria. Decomposition is clean.
4. **Ambiguity check:** specific file paths, specific API endpoints, specific Discord channel IDs (already in `secrets.yaml`).

No issues found.

## Next step

Hand off to `writing-plans` skill to produce the day-by-day actionable plan (each stage broken into ordered TodoList items with owner, estimated time, and dependency edges).
