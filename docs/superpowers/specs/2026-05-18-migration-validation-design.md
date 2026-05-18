# Migration Validation + Runner Hardening — Design

**Date:** 2026-05-18
**Status:** Shipped
**Author:** Claude (synthesis of Database Optimizer + DevOps Automator + Backend Architect specialist agents)

## Context

Over four sequential `make migrate` runs we hit four distinct Postgres errors,
each fixed only after deploying broken SQL into a live container:

| # | File | Error | Class |
|---|------|-------|-------|
| 1 | V001 | `uuid_generate_v4()` — missing `uuid-ossp` | Missing extension |
| 2 | V001 + V004 (×3 tables) | `PRIMARY KEY (col, COALESCE(...))` — function call inside inline PK | Constraint expression |
| 3 | V005 | `WHERE expires_at > NOW()` partial index — `NOW()` not IMMUTABLE | Index predicate purity |
| 4 | V004 | `COALESCE(from_state::text, '')` UNIQUE INDEX — enum→text cast is STABLE | Index predicate purity |

Recovery cost per defect: `down --volumes && up && migrate` — full chain
replay against fresh DB. Burned ~4× cycles for one V005 line today.

## Root cause

Migration SQL is only validated by deploying it. There is no
replay-against-throwaway-DB step in the local dev loop. Every defect
surfaces in prod-shape Postgres, not at author time. Static linters
(sqlfluff, pgsanity) do not catch the failure class — these are semantic
errors, not syntactic.

## Design — three layers

### Layer 3 — Preempt latent V001 defects (shipped first)

Audit by Database Optimizer agent surfaced three lurking bugs in V001
that would have fired sequentially across the next 1–3 migrate cycles:

- **V001:12** — `gin_trgm_ops` operator class used by `idx_opps_company_trgm`
  without `CREATE EXTENSION pg_trgm`. Apply would fail with
  `operator class gin_trgm_ops does not exist`.
- **V001:12** — `gen_random_uuid()` works on stock PG16 but is in
  `pg_catalog` only when the server build includes pgcrypto. Defensive
  `CREATE EXTENSION IF NOT EXISTS pgcrypto` removes ambiguity for any
  restore into a stripped PG image.
- **V001:209** — IVFFlat index built on an empty `opportunities` table
  produces degenerate centroids. Recall stays garbage until a manual
  `REINDEX` post-ingest. Silent perf bug. Swapped to HNSW
  (`m = 16, ef_construction = 64`) — no training step, recall stable
  from row #1.

Also during validation, **V004:18** failed: enum→text cast (`from_state::text`)
in UNIQUE INDEX predicate is STABLE, not IMMUTABLE. Replaced with two partial
indexes: `(from_state, to_state) WHERE from_state IS NOT NULL` and
`(to_state) WHERE from_state IS NULL`. Same logical uniqueness, no function
calls.

### Layer 2 — Runner hardening (`src/cli/main.py`)

- **Postgres advisory lock `(727274)`** held for the full migrate loop.
  Two concurrent `docker compose run --rm tools migrate` invocations now
  serialise instead of racing on `schema_migrations` bookkeeping. Released
  automatically on connection drop.
- **`_format_pg_error(err, sql, filename)`** — on `asyncpg.PostgresError`,
  translates the 1-based byte `position` into `file:line:col` plus the
  offending source line and a caret. Cuts debug time from "read 400 lines"
  to "fix line 23".
- **Docstring + log hint** — every failed file emits:
  > `[hint] fix the SQL and re-run mp migrate — no volume wipe needed; failed file rolled back cleanly.`
  Kills the wipe-volume ritual. Each V*.sql wraps in BEGIN/COMMIT and
  inserts its own `schema_migrations` marker INSIDE that transaction;
  failed files roll back marker and statements together, so re-running
  migrate replays cleanly from the failed file onwards.

### Layer 1 — Validation gate

`scripts/validate_migrations.sh` (new, executable):

1. `docker run -d --rm --tmpfs /var/lib/postgresql/data pgvector/pgvector:pg16`
2. Polls `pg_isready` until ready (≤30s)
3. Iterates `migrations/V*.sql` sorted by V-number
4. Pipes each into `psql -v ON_ERROR_STOP=1` (per-file BEGIN/COMMIT already
   in the SQL file)
5. Cleans up container on EXIT / INT / TERM via `trap`

Wired into two entry points sharing the script (single source of truth):

- **Pre-commit hook** (`.pre-commit-config.yaml`) — auto-fires whenever
  `migrations/V[0-9]+__.*\.sql` is staged. ~3–15s commit overhead.
- **`make migrate-test`** — manual ad-hoc runs (rebases, squash merges,
  spec-writing).

Why ephemeral pgvector + tmpfs:

- Real PG engine catches the failure class static linters miss
  (NOW() in partial index, enum cast in index, missing extension,
  ordering bugs).
- tmpfs data dir keeps validation fast and makes prod durability
  config (`synchronous_commit=on`, `full_page_writes=on`) irrelevant —
  this container never touches disk.
- `pgvector/pgvector:pg16` is multi-arch (amd64 + arm64) → identical
  behaviour on laptop and Pi 5.

## Architecture

```
┌───────────────────────────────────────────────────────────────┐
│  Author edits migrations/V00X__*.sql                          │
└────────────────────────────────┬──────────────────────────────┘
                                 │
                                 ▼
              ┌──────────────────────────────────────┐
              │  Layer 1 — pre-commit migrate-replay │
              │  scripts/validate_migrations.sh      │
              │  → ephemeral pgvector/pgvector:pg16  │
              │  → tmpfs data dir                    │
              │  → psql ON_ERROR_STOP=1 per file     │
              └──────────────────┬───────────────────┘
                       fail │    │ pass
                            ▼    ▼
                       (block)  commit + push
                                 │
                                 ▼
              ┌──────────────────────────────────────┐
              │  Layer 2 — src/cli/main.py migrate   │
              │  pg_advisory_lock(727274)            │
              │  for each unapplied V*.sql:          │
              │    conn.execute(sql)                 │
              │  on PostgresError →                  │
              │    _format_pg_error(file:line:col)   │
              │    + "no wipe needed" hint           │
              └──────────────────────────────────────┘
```

## Files changed

- `migrations/V001__core_schema.sql` — add `pg_trgm` + `pgcrypto` extensions;
  swap IVFFlat → HNSW
- `migrations/V004__opp_state_machine.sql` — replace COALESCE UNIQUE INDEX
  with two partial indexes
- `migrations/V005__cf_clearance_cache_indexes.sql` — drop `WHERE NOW()`
  predicate, use plain `(source_id, expires_at)` index
- `src/cli/main.py` — advisory lock, file:line error renderer, no-wipe hint,
  no-wipe docstring
- `scripts/validate_migrations.sh` — new, ephemeral pgvector replay
- `Makefile` — add `migrate-test` target
- `.pre-commit-config.yaml` — add `migrate-replay` local hook

## Failure modes covered

| Failure | Layer caught by | Behaviour |
|---|---|---|
| Author writes non-IMMUTABLE in index predicate | L1 pre-commit | Block commit, exit 1 |
| Author writes function call in inline PK | L1 pre-commit | Block commit, exit 1 |
| Author uses operator class without CREATE EXTENSION | L1 pre-commit | Block commit, exit 1 |
| Author swaps migration ordering (V005 references V006 table) | L1 pre-commit | Block commit, exit 1 |
| Two devs/CI workers run migrate concurrently | L2 advisory lock | Serialised, no corruption |
| Migrate fails on file N | L2 runner | Roll back N, keep V1..V(N-1), no wipe |
| Migrate dies mid-loop (SIGKILL on Pi power loss) | L2 advisory lock | Lock auto-released on conn drop, retry safe |
| Engineer wastes 30 min debugging "where in V001 did it fail" | L2 file:line renderer | Exact line + caret + class name |

## Out of scope

- Server-side CI gate (no GitHub Actions yet). L1 pre-commit covers local;
  if/when CI is added, the same script runs as a CI job — single source of
  truth.
- Down/rollback migrations. Forward-only; recovery is `pg_dump` restore.
- Static linter (sqlfluff/pgsanity). Disqualified — misses the actual
  failure class.
- Dry-run on prod DB. Ephemeral replay is the equivalent and faster.

## Verification

```
$ bash scripts/validate_migrations.sh
✓ all 6 migrations replay clean against pgvector/pgvector:pg16

$ make migrate
[apply] V001__core_schema.sql
[apply] V002__reserved_names_v2.sql
[apply] V003__sources_seed.sql
[apply] V004__opp_state_machine.sql
[apply] V005__cf_clearance_cache_indexes.sql
[apply] V006__digest_schedule.sql

$ docker compose exec postgres psql -U Cartograph -d Cartogrph -c '\dt'
→ 27 tables

$ ... SELECT count(*) FROM sources;
→ 29   (V003 has exactly 29 SELECT _seed_source() calls — verified
        2026-05-18; the "30" referenced in chat handoff was an
        aspirational number from the long-form plan, not the seeded
        reality. CLAUDE.md says "28+ sources" which is accurate.)
```

## Follow-ups (not in this design)

- ~~Investigate why `sources` count is 29 vs 30 expected.~~ Resolved
  2026-05-18: V003 has exactly 29 `SELECT _seed_source(...)` calls with no
  duplicates and no `ON CONFLICT` collisions. The "30" was an aspirational
  number from the long-form plan handoff, not the actual seeded reality.
- When V007 (resume_artifacts) lands, the L1 gate will validate it before
  commit.
- Per Backend Architect agent: move `INSERT INTO schema_migrations` from
  each SQL file into the runner, strip BEGIN/COMMIT + marker from V00X
  files. Single source of truth. Deferred — requires one-shot rewrite of
  6 files for zero behaviour change.
- `pre-commit install` was never run on this machine — the `migrate-replay`
  hook will only auto-fire after that one-time command. `make migrate-test`
  is always available as the manual entry point.
- Test suite (`make test` / `pytest`) was not invoked during this work.
  All verification was via live container logs + DB state. Worth a pass
  before declaring Phase 1 done.
