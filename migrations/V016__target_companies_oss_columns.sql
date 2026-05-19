-- V016__target_companies_oss_columns.sql
-- Phase 3.4 — OSS contribution funnel.
--
-- Extends the V002-reserved / V010-evolved `target_companies` table with
-- the columns the OSS funnel worker needs to query GitHub Search per
-- target company and rate-limit emissions.
--
-- Why these columns:
--   * `github_org` — without an org slug we have nothing to ask the
--     GitHub Search API for. NULLABLE because the Phase 2.1 cold-outreach
--     consumer adds target_companies rows with only a domain set, and we
--     don't want this migration to break that path. The OSS worker
--     filters to `WHERE github_org IS NOT NULL` at query time.
--   * `active` — soft on/off switch the user controls via `mp targets
--     pause <name>`. Default TRUE so newly added rows are scanned
--     immediately. The partial index on this column keeps the scheduler
--     query cheap when the user accumulates many archived companies.
--   * `last_funnel_scan_at` — bookkeeping for /status + a future
--     incremental scan optimization. Updated each cron tick regardless
--     of whether we found new issues.
--   * `issues_emitted_30d` — exposed by /status so the user can see at
--     a glance how productive the funnel has been per target. Updated
--     by the worker after each scan (rolling 30d count).
--
-- Hard rules:
--   1. `ADD COLUMN IF NOT EXISTS` so a re-run is a no-op.
--   2. Both indexes are partial / lightweight — the table is small
--      (~tens of rows in Phase 3) and queries are scheduler-driven
--      (once-per-day) not user-driven, so we err on the side of fewer
--      indexes.
--   3. No FK on `github_org` to a fictional `github_orgs` table — the
--      org slug is a free-form string, validated structurally in
--      the CLI before insert.
--   4. Phase 2.1 cold-outreach (sender.py) reads `id, name, domain,
--      mission_summary, why_target` — none of those are touched here,
--      so that consumer is untouched.
-- ---------------------------------------------------------------

BEGIN;

ALTER TABLE target_companies
    ADD COLUMN IF NOT EXISTS github_org          TEXT,
    ADD COLUMN IF NOT EXISTS active              BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS last_funnel_scan_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS issues_emitted_30d  INT NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_target_companies_active
    ON target_companies(active);

CREATE INDEX IF NOT EXISTS idx_target_companies_github_org
    ON target_companies(github_org)
    WHERE github_org IS NOT NULL;

-- Seed a single synthetic `sources` row for the OSS funnel. Every
-- Opportunity row needs a source_id NOT NULL FK target; the funnel
-- bypasses the FetchTask/FetchResult cycle (the GitHub Search API
-- output is already structured enough to skip the extractor cascade),
-- so we never register a crawler_strategy plugin — the seed row
-- exists only to satisfy the FK and to surface OSS-funnel emissions
-- in `/source list`. status='active' but fetch_freq_minutes=10080
-- (weekly) so the normal scheduler tick in workers/scheduler.py
-- ignores it; the OSS funnel cron is the actual driver.
-- Direct insert because the V003 `_seed_source` helper was dropped at
-- the end of V003. status='paused' so the scheduler's normal
-- emit_for_active_sources tick (`WHERE status='active'`) does NOT
-- try to dispatch a FetchTask via a non-existent 'oss_funnel'
-- crawler_strategy. The dedicated OSS funnel worker drives off
-- target_companies + its own cron and only needs this row to satisfy
-- opportunities.source_id FK on insert.
INSERT INTO sources (
    slug, name, category, base_url, crawler_strategy,
    fetch_freq_minutes, priority, cf_protection_level,
    tier_chain, browser_mode_required, status, created_via
) VALUES (
    'oss_funnel',
    'GitHub good-first-issue funnel',
    'other',
    'https://api.github.com',
    'oss_funnel',
    10080,           -- 7 days; the dedicated cron drives the real cadence
    7,
    'none',
    ARRAY[0]::INT[],
    FALSE,
    'paused',
    'seed'
)
ON CONFLICT (slug) DO UPDATE
    SET category = EXCLUDED.category,
        base_url = EXCLUDED.base_url,
        status   = 'paused';

-- Phase 3.4 demo seed. ON CONFLICT (user_id, lower(domain)) — the V010
-- unique index — keeps reruns idempotent and respects an existing row
-- the user may already have for either company (e.g. via cold-outreach
-- seeding). user_id defaults to 1 (solo-owner contract).
INSERT INTO target_companies (name, domain, github_org, why_target) VALUES
    ('Vercel',    'vercel.com',    'vercel',    'frontend infra; user is Pi-grade backend, complementary skills'),
    ('Anthropic', 'anthropic.com', 'anthropics', 'Claude tooling adjacent to user pipeline; portfolio fit')
ON CONFLICT (user_id, (lower(domain))) WHERE domain IS NOT NULL
DO UPDATE SET
    github_org = COALESCE(target_companies.github_org, EXCLUDED.github_org),
    active     = TRUE;

INSERT INTO schema_migrations (version) VALUES ('V016') ON CONFLICT DO NOTHING;

COMMIT;
