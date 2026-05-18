-- V001__core_schema.sql
-- Marked_Path Phase 1 core schema. Idempotent (CREATE IF NOT EXISTS where possible).
-- Multi-tenant-ready: every user-scoped row has user_id with DEFAULT 1 for solo phase.

BEGIN;

-- =========================================================================
-- Extensions (must be loaded before any vector(N) column declaration).
-- pgvector image (pgvector/pgvector:pg16) provides the shared library;
-- this statement enables the SQL type for this database.
-- =========================================================================
CREATE EXTENSION IF NOT EXISTS vector;

-- =========================================================================
-- Migration bookkeeping
-- =========================================================================
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- =========================================================================
-- Users + identities
-- =========================================================================
CREATE TABLE IF NOT EXISTS users (
    id            BIGSERIAL PRIMARY KEY,
    handle        TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    timezone      TEXT NOT NULL DEFAULT 'Asia/Kolkata',
    status        TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','paused','disabled')),
    tier          TEXT NOT NULL DEFAULT 'solo'
                    CHECK (tier IN ('solo','beta','team')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Default solo user (id=1). Multi-tenant cutover (Phase 4) drops the DEFAULT.
INSERT INTO users (id, handle, display_name)
VALUES (1, 'owner', 'Owner')
ON CONFLICT (id) DO NOTHING;
SELECT setval('users_id_seq', GREATEST((SELECT MAX(id) FROM users), 1));

CREATE TABLE IF NOT EXISTS fingerprints (
    id                BIGSERIAL PRIMARY KEY,
    ua_string         TEXT NOT NULL,
    viewport          TEXT,
    timezone          TEXT,
    locale            TEXT,
    webgl_hash        TEXT,
    canvas_hash       TEXT,
    font_set_hash     TEXT,
    last_assigned_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS identities (
    id                       BIGSERIAL PRIMARY KEY,
    user_id                  BIGINT NOT NULL DEFAULT 1 REFERENCES users(id) ON DELETE CASCADE,
    platform                 TEXT NOT NULL,
    account_label            TEXT NOT NULL,
    encrypted_credentials    BYTEA,
    encrypted_cookies        BYTEA,
    cookie_nonce             BYTEA,
    cred_nonce               BYTEA,
    fingerprint_id           BIGINT REFERENCES fingerprints(id) ON DELETE SET NULL,
    proxy_sticky_session_id  TEXT,
    email_alias              TEXT,
    last_used_at             TIMESTAMPTZ,
    ban_status               TEXT NOT NULL DEFAULT 'healthy'
                              CHECK (ban_status IN ('healthy','suspect','quarantined','banned')),
    warmup_score             REAL NOT NULL DEFAULT 0,
    warmup_completed         BOOLEAN NOT NULL DEFAULT FALSE,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (platform, account_label)
);
CREATE INDEX IF NOT EXISTS idx_identities_user ON identities(user_id);
CREATE INDEX IF NOT EXISTS idx_identities_platform_ban ON identities(platform, ban_status);
CREATE INDEX IF NOT EXISTS idx_identities_fingerprint ON identities(fingerprint_id);

CREATE TABLE IF NOT EXISTS user_identities (
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    identity_id BIGINT NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('owner','borrower')),
    granted_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, identity_id)
);
-- One owner per identity
CREATE UNIQUE INDEX IF NOT EXISTS uniq_identity_owner
    ON user_identities (identity_id)
    WHERE role = 'owner';

CREATE TABLE IF NOT EXISTS identity_checkouts (
    id           BIGSERIAL PRIMARY KEY,
    identity_id  BIGINT NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
    worker_id    TEXT NOT NULL,
    leased_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at   TIMESTAMPTZ NOT NULL,
    returned_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_checkouts_active
    ON identity_checkouts(identity_id)
    WHERE returned_at IS NULL;

CREATE TABLE IF NOT EXISTS identity_audit (
    id           BIGSERIAL PRIMARY KEY,
    identity_id  BIGINT NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
    action       TEXT NOT NULL,
    actor        TEXT NOT NULL,
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata     JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_audit_identity_time
    ON identity_audit(identity_id, occurred_at DESC);

-- =========================================================================
-- Sources
-- =========================================================================
CREATE TABLE IF NOT EXISTS sources (
    id                          BIGSERIAL PRIMARY KEY,
    slug                        TEXT NOT NULL UNIQUE,
    name                        TEXT NOT NULL,
    category                    TEXT NOT NULL
                                  CHECK (category IN
                                    ('ats','rss','github_md','hn','reddit',
                                     'fellowship','india','freelance','other')),
    base_url                    TEXT NOT NULL,
    crawler_strategy            TEXT NOT NULL,
    fetch_freq_minutes          INTEGER NOT NULL DEFAULT 60,
    priority                    INTEGER NOT NULL DEFAULT 5,
    robots_respected            BOOLEAN NOT NULL DEFAULT TRUE,
    ban_observed_at             TIMESTAMPTZ,
    auth_account_id             BIGINT REFERENCES identities(id) ON DELETE SET NULL,
    ranking_weight              REAL NOT NULL DEFAULT 1.0,
    created_via                 TEXT NOT NULL DEFAULT 'seed',
    discovery_candidate_id      BIGINT,
    discovery_confidence        REAL,
    status                      TEXT NOT NULL DEFAULT 'active'
                                  CHECK (status IN ('active','paused','quarantined','disabled')),
    last_successful_crawl_at    TIMESTAMPTZ,
    opps_extracted_30d          INTEGER NOT NULL DEFAULT 0,
    requires_residential        BOOLEAN NOT NULL DEFAULT FALSE,
    browser_mode_required       BOOLEAN NOT NULL DEFAULT FALSE,
    tier_chain                  INTEGER[] NOT NULL DEFAULT ARRAY[0],
    cf_protection_level         TEXT NOT NULL DEFAULT 'none'
                                  CHECK (cf_protection_level IN ('none','basic','managed','enterprise')),
    last_cf_challenge_at        TIMESTAMPTZ,
    daily_cost_budget_cents     INTEGER NOT NULL DEFAULT 0,
    config                      JSONB NOT NULL DEFAULT '{}'::jsonb,
    notes                       TEXT,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sources_status_priority ON sources(status, priority DESC);
CREATE INDEX IF NOT EXISTS idx_sources_category ON sources(category);

-- =========================================================================
-- Opportunities
-- =========================================================================
DO $$ BEGIN
    CREATE TYPE apply_method_enum AS ENUM
        ('email','ats_form','external','in_platform','embedded_form');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE opp_state_enum AS ENUM
        ('new','queued','ranked','digested','seen','snoozed',
         'applied','interview','offer','rejected','withdrawn','expired');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE remote_type_enum AS ENUM
        ('remote','hybrid','onsite','unspecified');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS opportunities (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id                BIGINT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    canonical_url            TEXT NOT NULL UNIQUE,
    title                    TEXT NOT NULL,
    company                  TEXT,
    description              TEXT,
    comp_min                 NUMERIC(12,2),
    comp_max                 NUMERIC(12,2),
    comp_currency            TEXT,
    comp_period              TEXT,  -- hour|month|year
    location                 TEXT,
    remote_type              remote_type_enum NOT NULL DEFAULT 'unspecified',
    category                 TEXT NOT NULL DEFAULT 'unknown'
                              CHECK (category IN
                                ('fulltime','internship','fellowship','freelance','contract','unknown')),
    posted_at                TIMESTAMPTZ,
    expires_at               TIMESTAMPTZ,
    apply_url                TEXT,
    apply_method             apply_method_enum,
    raw_payload_s3_key       TEXT,
    fingerprint_hash         TEXT NOT NULL,
    embedding                vector(384),
    state                    opp_state_enum NOT NULL DEFAULT 'new',
    first_seen               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    extraction_tier          SMALLINT NOT NULL DEFAULT 0,
    extraction_confidence    REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_opps_source_state ON opportunities(source_id, state);
CREATE INDEX IF NOT EXISTS idx_opps_state_posted ON opportunities(state, posted_at DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_opps_first_seen ON opportunities(first_seen DESC);
CREATE INDEX IF NOT EXISTS idx_opps_fingerprint ON opportunities(fingerprint_hash);
CREATE INDEX IF NOT EXISTS idx_opps_company_trgm ON opportunities USING gin (company gin_trgm_ops);
-- Embedding cosine index (IVFFlat — rebuild after large ingest)
CREATE INDEX IF NOT EXISTS idx_opps_embedding
    ON opportunities USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

CREATE TABLE IF NOT EXISTS opportunity_scores (
    user_id           BIGINT NOT NULL DEFAULT 1 REFERENCES users(id) ON DELETE CASCADE,
    opportunity_id    UUID NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    score             REAL NOT NULL,
    score_components  JSONB NOT NULL DEFAULT '{}'::jsonb,
    scored_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ranker_version    TEXT NOT NULL DEFAULT 'v1',
    PRIMARY KEY (user_id, opportunity_id)
);
CREATE INDEX IF NOT EXISTS idx_opp_scores_user_score
    ON opportunity_scores(user_id, score DESC);

CREATE TABLE IF NOT EXISTS opportunity_transitions (
    id              BIGSERIAL PRIMARY KEY,
    opportunity_id  UUID NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    from_state      opp_state_enum,
    to_state        opp_state_enum NOT NULL,
    trigger         TEXT NOT NULL,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_transitions_opp_time
    ON opportunity_transitions(opportunity_id, occurred_at DESC);

-- =========================================================================
-- Profiles
-- =========================================================================
CREATE TABLE IF NOT EXISTS profiles (
    id                BIGSERIAL PRIMARY KEY,
    user_id           BIGINT NOT NULL DEFAULT 1 REFERENCES users(id) ON DELETE CASCADE,
    embedding         vector(384),
    headline          TEXT,
    skills            TEXT[] NOT NULL DEFAULT '{}',
    target_lanes      TEXT[] NOT NULL DEFAULT '{}',
    min_comp_usd_hr   NUMERIC(8,2),
    geo_pref          TEXT,
    raw_resume        JSONB,
    raw_skills_yaml   JSONB,
    raw_prefs_yaml    JSONB,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id)
);

-- =========================================================================
-- Applications
-- =========================================================================
CREATE TABLE IF NOT EXISTS applications (
    id                  BIGSERIAL PRIMARY KEY,
    user_id             BIGINT NOT NULL DEFAULT 1 REFERENCES users(id) ON DELETE CASCADE,
    opportunity_id      UUID NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    sent_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    method              apply_method_enum NOT NULL,
    resume_variant_id   BIGINT,  -- FK added in Phase 2
    cover_letter_id     BIGINT,
    payload             JSONB NOT NULL DEFAULT '{}'::jsonb,
    response_status     TEXT,
    response_at         TIMESTAMPTZ,
    discord_thread_id   BIGINT,
    UNIQUE (user_id, opportunity_id)
);
CREATE INDEX IF NOT EXISTS idx_apps_user_sent ON applications(user_id, sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_apps_response ON applications(response_status);

-- =========================================================================
-- Notification routes
-- =========================================================================
DO $$ BEGIN
    CREATE TYPE notification_channel_enum AS ENUM ('discord','obsidian','email','telegram');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE route_type_enum AS ENUM
        ('daily_digest','priority_push','tracker','alerts','costs');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS notification_routes (
    user_id              BIGINT NOT NULL DEFAULT 1 REFERENCES users(id) ON DELETE CASCADE,
    channel              notification_channel_enum NOT NULL,
    route_type           route_type_enum NOT NULL,
    target               TEXT NOT NULL,
    enabled              BOOLEAN NOT NULL DEFAULT TRUE,
    quiet_hours          int4range,
    discord_channel_id   BIGINT,
    discord_thread_id    BIGINT,
    embed_color          INTEGER,
    PRIMARY KEY (user_id, channel, route_type)
);

-- =========================================================================
-- CF clearance cache
-- =========================================================================
CREATE TABLE IF NOT EXISTS cf_clearance_cache (
    id               BIGSERIAL PRIMARY KEY,
    source_id        BIGINT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    identity_id      BIGINT REFERENCES identities(id) ON DELETE SET NULL,
    domain           TEXT NOT NULL,
    cookie_value     TEXT NOT NULL,
    ua_string        TEXT NOT NULL,
    ja4_profile      TEXT,
    ip_solved_from   INET,
    acquired_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at       TIMESTAMPTZ NOT NULL,
    last_used_at     TIMESTAMPTZ,
    success_count    INTEGER NOT NULL DEFAULT 0,
    failure_count    INTEGER NOT NULL DEFAULT 0
);
-- Logical uniqueness on (source_id, identity_id, domain) where identity_id
-- can be NULL. Postgres treats NULL as not-equal in regular unique indexes,
-- so we use COALESCE to fold NULL into 0 for uniqueness purposes. The
-- expression form requires this to be a separate index, not an inline
-- PRIMARY KEY constraint (which only accepts bare column refs).
CREATE UNIQUE INDEX IF NOT EXISTS uniq_cf_clearance
    ON cf_clearance_cache (source_id, COALESCE(identity_id, 0), domain);

-- =========================================================================
-- Cost ledger
-- =========================================================================
DO $$ BEGIN
    CREATE TYPE usage_kind_enum AS ENUM
        ('llm_extract','llm_rerank','llm_writer','llm_classifier','embedding','proxy','captcha','other');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS usage_ledger (
    id                 BIGSERIAL PRIMARY KEY,
    user_id            BIGINT NOT NULL DEFAULT 1 REFERENCES users(id) ON DELETE CASCADE,
    ts                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    kind               usage_kind_enum NOT NULL,
    provider           TEXT NOT NULL,
    model              TEXT,
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cost_usd_micros    BIGINT NOT NULL DEFAULT 0,  -- 1 USD = 1_000_000
    correlation_id     TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_ledger(ts DESC);
CREATE INDEX IF NOT EXISTS idx_usage_user_kind_ts ON usage_ledger(user_id, kind, ts DESC);

CREATE TABLE IF NOT EXISTS daily_spend (
    id             BIGSERIAL PRIMARY KEY,
    date           DATE NOT NULL,
    source_id      BIGINT,
    tier           INTEGER NOT NULL DEFAULT 0,
    request_count  INTEGER NOT NULL DEFAULT 0,
    cents_spent    INTEGER NOT NULL DEFAULT 0
);
-- Logical uniqueness on (date, source_id, tier) where source_id can be NULL.
-- See cf_clearance_cache for the same expression-index rationale.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_daily_spend
    ON daily_spend (date, COALESCE(source_id, 0), tier);

-- =========================================================================
-- Source health rolling view
-- =========================================================================
CREATE OR REPLACE VIEW source_health_24h AS
SELECT
    s.id,
    s.slug,
    s.status,
    s.last_successful_crawl_at,
    COUNT(o.id) FILTER (WHERE o.first_seen >= NOW() - INTERVAL '24 hours') AS opps_24h,
    s.opps_extracted_30d,
    s.last_cf_challenge_at,
    EXTRACT(EPOCH FROM (NOW() - s.last_successful_crawl_at))/60 AS minutes_since_success
FROM sources s
LEFT JOIN opportunities o ON o.source_id = s.id
GROUP BY s.id;

-- =========================================================================
-- updated_at trigger helper
-- =========================================================================
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_sources_updated_at ON sources;
CREATE TRIGGER trg_sources_updated_at
    BEFORE UPDATE ON sources
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

INSERT INTO schema_migrations (version) VALUES ('V001') ON CONFLICT DO NOTHING;

COMMIT;
