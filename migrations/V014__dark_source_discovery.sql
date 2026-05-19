-- V013__dark_source_discovery.sql
-- Phase 3.2 — Dark-source discovery worker.
--
-- Materialises the v2 reserved-name tables (V002 stubs) into real schema:
--   * candidate_sources    — URLs mined by the 4 discovery strategies. Mid-
--                            confidence rows wait for /review approval.
--   * discovery_strategies — Per-strategy on/off + counters. Pausing one
--                            strategy (`name='google_dorks'`, active=FALSE)
--                            does not affect the other three.
--   * source_provenance    — Audit trail back from sources → candidate_sources.
--                            Many rows-per-source allowed (re-promotion).
--
-- Why ALTER ... ADD COLUMN IF NOT EXISTS instead of CREATE TABLE:
--   V002 already CREATE TABLE IF NOT EXISTS'd these with `placeholder TEXT`.
--   Bare CREATE TABLE here is a no-op on V002-applied DBs; we must use ALTER.
--
-- Idempotence: every ADD COLUMN + CREATE INDEX uses IF NOT EXISTS, so re-
-- running this file against a partially-migrated DB lands in the same state.

BEGIN;

-- ---------------------------------------------------------------------------
-- candidate_sources — the queue of URLs awaiting human/auto review.
-- ---------------------------------------------------------------------------
ALTER TABLE candidate_sources
    ADD COLUMN IF NOT EXISTS url                   TEXT,
    ADD COLUMN IF NOT EXISTS title                 TEXT,
    ADD COLUMN IF NOT EXISTS snippet               TEXT,
    ADD COLUMN IF NOT EXISTS discovered_via        TEXT,
    ADD COLUMN IF NOT EXISTS classifier_confidence REAL,
    ADD COLUMN IF NOT EXISTS classifier_category   TEXT,
    ADD COLUMN IF NOT EXISTS classifier_rationale  TEXT,
    ADD COLUMN IF NOT EXISTS status                TEXT
                                CHECK (status IN ('pending','approved','rejected','snoozed','auto_promoted'))
                                DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS created_at            TIMESTAMPTZ DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS reviewed_at           TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS promoted_source_id    BIGINT REFERENCES sources(id) ON DELETE SET NULL;

-- Unique on url so dedupe is enforced at the DB layer (the worker also
-- pre-filters in Python, but a second writer must never insert a dup).
CREATE UNIQUE INDEX IF NOT EXISTS uniq_candidate_url ON candidate_sources(url);
CREATE INDEX IF NOT EXISTS idx_candidate_status ON candidate_sources(status);
CREATE INDEX IF NOT EXISTS idx_candidate_created ON candidate_sources(created_at DESC);

-- ---------------------------------------------------------------------------
-- discovery_strategies — per-strategy on/off switch + counters.
-- ---------------------------------------------------------------------------
ALTER TABLE discovery_strategies
    ADD COLUMN IF NOT EXISTS name             TEXT,
    ADD COLUMN IF NOT EXISTS active           BOOLEAN DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS last_run_at      TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS discovered_count INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS promoted_count   INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS discarded_count  INTEGER DEFAULT 0;

CREATE UNIQUE INDEX IF NOT EXISTS uniq_discovery_name ON discovery_strategies(name);

-- ---------------------------------------------------------------------------
-- source_provenance — audit trail from sources → candidate_sources.
-- ---------------------------------------------------------------------------
ALTER TABLE source_provenance
    ADD COLUMN IF NOT EXISTS source_id           BIGINT REFERENCES sources(id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS candidate_source_id BIGINT REFERENCES candidate_sources(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS discovered_via      TEXT,
    ADD COLUMN IF NOT EXISTS promoted_at         TIMESTAMPTZ DEFAULT NOW();

CREATE INDEX IF NOT EXISTS idx_provenance_source ON source_provenance(source_id);
CREATE INDEX IF NOT EXISTS idx_provenance_candidate ON source_provenance(candidate_source_id);

-- ---------------------------------------------------------------------------
-- Seed the 4 discovery strategies. All active by default; user pauses via
-- /review or direct UPDATE.
-- ---------------------------------------------------------------------------
INSERT INTO discovery_strategies (name) VALUES
    ('github_awesome_lists'),
    ('hn_algolia_search'),
    ('reddit_search'),
    ('google_dorks')
ON CONFLICT (name) DO NOTHING;

INSERT INTO schema_migrations (version) VALUES ('V013') ON CONFLICT DO NOTHING;

COMMIT;
