-- V015__twitter_source.sql
-- Phase 3.1 — Twitter/X founder-signal scraper meta-row.
--
-- The freelance-twitter-fetcher worker polls a configurable list of Nitter
-- instances (mirrors of Twitter without auth) on a 30 min schedule, filters
-- new tweets from user-curated founder/recruiter handles for hiring-intent
-- keywords, and publishes matches directly onto stream:rank — same shape as
-- the freelance-telegram-fetcher lane. The Twitter source row exists ONLY so
-- that opportunities can carry a stable source_id; the URL column points at
-- a representative Nitter mirror but is informational — the worker does NOT
-- consume `sources.base_url` from this row. The crawler / extractor pipeline
-- never sees this row's strategy because the SourcePlugin emits an empty URL
-- list (see src/sources/freelance/twitter_signal.py).
--
-- Why a single row rather than one per handle:
--   - Handles live in config/profile/prefs.yaml -> freelance.twitter_handles
--     and the user MUST be able to add/remove handles without a migration.
--   - source_quality + ranking_weight are tuned per-source family; per-handle
--     calibration is Phase 4+ territory.
--
-- Hard rules (CLAUDE.md):
--   * slug + name are NOT NULL in V001 — both populated below.
--   * category enum is constrained — 'freelance' matches the existing fl_*
--     family.
--   * ON CONFLICT DO NOTHING — re-applying V013 is a no-op if the operator
--     manually re-seeded sources beforehand.

BEGIN;

INSERT INTO sources (
    slug,
    name,
    category,
    base_url,
    crawler_strategy,
    fetch_freq_minutes,
    priority,
    cf_protection_level,
    tier_chain,
    browser_mode_required,
    status,
    created_via,
    notes
) VALUES (
    'fl_twitter_signal',
    'Twitter/X founder signal (via Nitter)',
    'freelance',
    'https://nitter.net',
    'twitter_founder_signal',
    30,
    7,
    'none',
    ARRAY[0],
    FALSE,
    'active',
    'seed',
    'Phase 3.1 Twitter/X founder signal. Polled by src/workers/twitter_signal.py via Nitter mirrors — does NOT use the HTTP crawler. Handles list lives in config/profile/prefs.yaml -> freelance.twitter_handles. base_url is informational; actual mirrors rotate via NITTER_INSTANCES in the fetcher.'
)
ON CONFLICT (slug) DO NOTHING;

INSERT INTO schema_migrations (version) VALUES ('V015') ON CONFLICT DO NOTHING;

COMMIT;
