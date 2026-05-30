# Internshala JOBS discovery worker

**Shipped 2026-05-31.** Sibling of the camoufox internship discovery worker, for
Internshala's full-time **jobs** pages. Enforces a 12 LPA strict lower-bound
salary floor (Internshala's UI salary filter caps at 10 LPA) plus an experience
cap the listing UI can't express, with currency conversion before the floor.

## Decisions (user-confirmed)

| # | Decision |
|---|---|
| Architecture | Isolated sibling package `src/workers/internshala_jobs_discovery/` that imports the shared pure helpers; the internship worker is untouched. |
| Experience | Parse min-years from the card; keep iff `min ≤ max_experience_years` (prefs, **default 1**). Fail-open on missing experience. |
| URL variants | Crawl **both** `/jobs/…` and `/fresher-jobs/…` each cycle; shared Redis dedup keyspace collapses overlap. |
| Salary floor | **Strict lower bound** — keep iff `comp_min` → INR/month ≥ 100000 (12 LPA ÷ 12). Differs from internships (range upper bound). |

Jobs filter via **URL path** (cities / work-from-home / salary / fresher), not
dropdowns. Live jobs DOM unverified → jobs selectors ship `version:
RECON_PENDING`; unit-tested against synthetic fixtures.

## Components

| Role | File |
|---|---|
| Experience parser (pure, corpus-tested) | `src/common/experience_parser.py` |
| Jobs card parser (FULLTIME + experience) | `src/sources/india/internshala_jobs_card_parser.py` |
| Jobs config + URL builder | `src/workers/internshala_jobs_discovery/config.py` (`build_variants`, `load_jobs_config`, `JobsDiscoveryConfig.salary_floor_inr`) |
| Jobs gates | `src/workers/internshala_jobs_discovery/filters.py` (`passes_salary_floor` strict-min, `passes_experience` fail-open) |
| Cycle (URL-variant iteration, no dropdowns) | `src/workers/internshala_jobs_discovery/cycle.py` |
| Jobs-bound cycle-log persistence | `src/workers/internshala_jobs_discovery/persistence.py` |
| Entrypoint (heartbeat `discovery:heartbeat:jobs`) | `src/workers/internshala_jobs_discovery_worker.py` |
| CLI | `src/cli/internshala_jobs_discover.py` (`carto internshala-jobs-discover`) |
| Selectors (RECON_PENDING) | `config/sources/internshala_jobs_selectors.yaml` |
| Prefs block | `config/profile/prefs.yaml` `discovery.internshala_jobs` |
| Compose service | `compose.sidecar.yaml` `internshala-jobs-discovery-worker` |
| Migration | `migrations/V028__internshala_jobs.sql` |

**Reused by import (unchanged):** internship `browser_ops` (scrape/paginate/
challenge/modal/sel), internship `report` (`passes_validity`, `dedup_key`,
`DiscoveryCycleReport`, payload builder), internship `persistence` (`_INSERT_CYCLE_LOG`,
`publish_notify`), `persist_and_publish`, `parse_stipend`, `parse_apply_by`/
`parse_posted_relative`, `to_inr_per_month`. `OppCategory.FULLTIME` already routes
to `#💼-fulltime` and the ranker already has a fulltime floor — no downstream work.

## Gate order (`_ingest_card`)

parse (jobs) → **salary floor (strict comp_min ≥ 12 LPA)** → **experience (≤ cap)**
→ validity (apply-by / age) → dry-run shortcut → Redis dedup → `persist_and_publish`.
Reject counters: parse / subfloor / experience / expired / dedup.

Currency conversion is automatic: `passes_salary_floor` runs `comp_min` through
`to_inr_per_month` (USD→INR 83x, year→÷12) before comparing to the floor.

## Shared additive edits (default-preserve internships)

- `Opportunity.years_experience_min: int | None` + the `persist_and_publish`
  INSERT column (all non-jobs producers write NULL).
- `DiscoveryCycleReport.cards_rejected_experience: int = 0` + the cycle-log
  INSERT column + the degraded #🛠-source-health embed line (internships emit 0).
- **V028** adds `opportunities.years_experience_min`,
  `discovery_cycle_log.cards_rejected_experience`, and the `in_internshala_jobs`
  source row (`category='india'` per CHECK; `discovery_method='camoufox_dropdown'`
  reused to avoid CHECK surgery).

## RECON dependency

`internshala_jobs_selectors.yaml` ships `RECON_PENDING` (worker refuses to boot
unless `INTERNSHALA_JOBS_ALLOW_RECON_PENDING=1`). All `listing` selectors —
especially `card_stipend` (salary) and `card_experience` — are placeholders;
confirm them live on the ThinkPad and bump the version before first deploy.

## Deploy notes / risks

- **Shared Internshala identity**: both workers `checkout(platform="internshala")`.
  If the vault is one-lease-per-platform, the 2nd worker exits 2 (handled). Use a
  second identity row, or confirm multi-lease, before running both concurrently.
- **Heartbeat keys namespaced** (`discovery:heartbeat:jobs`) so the two
  healthchecks don't clobber.
- Same image `marked_path-discovery:latest`; only the entrypoint module differs
  (compose `entrypoint:` override). No new Dockerfile.

## Verification (done)

- Unit: experience parser corpus, jobs card parser (fixtures), `build_variants` /
  `passes_salary_floor` strict-min / `passes_experience` / config tables — all green;
  full suite **636 passed** (1 pre-existing unrelated algora date-test fail).
- Migration: V001→V028 replayed clean against ephemeral pgvector; both new columns
  + the source row verified.
- `load_jobs_config` reads the real prefs (12 LPA→₹100k/mo, exp cap 1) and builds
  both variant URLs matching the user's links.
- `docker compose -f compose.sidecar.yaml config` valid.
- Live recon + dry-run smoke (`carto internshala-jobs-discover --once --dry-run`
  with `INTERNSHALA_JOBS_ALLOW_RECON_PENDING=1`) pending on the ThinkPad.
