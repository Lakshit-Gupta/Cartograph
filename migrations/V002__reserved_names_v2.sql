-- V002__reserved_names_v2.sql
-- Reserves v2 table names so Phase 3+ migrations don't collide.
-- Tables are empty + minimal; real schema lands in V0XX migrations.

BEGIN;

CREATE TABLE IF NOT EXISTS candidate_sources (
    id          BIGSERIAL PRIMARY KEY,
    placeholder TEXT
);

CREATE TABLE IF NOT EXISTS discovery_strategies (
    id          BIGSERIAL PRIMARY KEY,
    placeholder TEXT
);

CREATE TABLE IF NOT EXISTS source_provenance (
    id          BIGSERIAL PRIMARY KEY,
    placeholder TEXT
);

CREATE TABLE IF NOT EXISTS resume_variants (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT,
    label       TEXT,
    placeholder TEXT
);

CREATE TABLE IF NOT EXISTS target_companies (
    id          BIGSERIAL PRIMARY KEY,
    placeholder TEXT
);

CREATE TABLE IF NOT EXISTS contacts (
    id          BIGSERIAL PRIMARY KEY,
    placeholder TEXT
);

CREATE TABLE IF NOT EXISTS outreach_log (
    id          BIGSERIAL PRIMARY KEY,
    placeholder TEXT
);

INSERT INTO schema_migrations (version) VALUES ('V002') ON CONFLICT DO NOTHING;

COMMIT;
