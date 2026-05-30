-- V028__internshala_jobs.sql
-- Internshala full-time JOBS discovery vertical.
--
-- Context (2026-05-31): a sibling of the camoufox internship discovery worker,
-- for Internshala's /jobs/ + /fresher-jobs/ pages. Enforces a 12 LPA strict
-- lower-bound salary floor (Internshala's UI salary filter caps at 10 LPA) and
-- an experience cap the listing UI can't express. See the plan + the jobs
-- discovery package src/workers/internshala_jobs_discovery/.
--
-- All additive:
--   1. opportunities.years_experience_min — the min required experience parsed
--      from a jobs card (NULL for internships + every non-jobs producer).
--   2. discovery_cycle_log.cards_rejected_experience — the jobs-only reject
--      counter (DEFAULT 0 so existing rows + internship cycles are unaffected).
--   3. the in_internshala_jobs source row (category 'india' per the CHECK;
--      discovery_method reuses 'camoufox_dropdown' to avoid CHECK surgery —
--      the column's only consumer is the Pi scheduler's != 'http_curl' gate).

BEGIN;

ALTER TABLE opportunities
  ADD COLUMN IF NOT EXISTS years_experience_min SMALLINT NULL;

ALTER TABLE discovery_cycle_log
  ADD COLUMN cards_rejected_experience INT NOT NULL DEFAULT 0;

INSERT INTO sources (
    slug, name, category, base_url, crawler_strategy,
    fetch_freq_minutes, priority, status, robots_respected,
    ranking_weight, browser_mode_required, tier_chain,
    cf_protection_level, discovery_method, notes
) VALUES (
    'in_internshala_jobs', 'Internshala Jobs', 'india',
    'https://internshala.com/jobs', 'india_internshala_jobs',
    60, 8, 'active', TRUE,
    1.0, FALSE, ARRAY[0, 1],
    'basic', 'camoufox_dropdown',
    'Phase 4.x — Internshala full-time jobs via camoufox URL crawl; 12 LPA strict-min floor + experience filter.'
) ON CONFLICT (slug) DO NOTHING;

INSERT INTO schema_migrations (version) VALUES ('V028') ON CONFLICT DO NOTHING;

COMMIT;
