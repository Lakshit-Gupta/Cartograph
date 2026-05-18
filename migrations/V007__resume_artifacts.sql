-- V007__resume_artifacts.sql
-- Adds tracking columns to `applications` and the `resume_compile_log` table
-- required by the LaTeX resume subsystem (CLAUDE.md ratified design).
--
-- Multi-tenant ready: every new row carries user_id NOT NULL DEFAULT 1.
-- See docs/superpowers/plans/2026-05-18-phase01-closeout-plan.md Task 4.1.

BEGIN;

ALTER TABLE applications
    ADD COLUMN IF NOT EXISTS resume_artifact_sha256 CHAR(64),
    ADD COLUMN IF NOT EXISTS resume_source_hash    CHAR(64),
    ADD COLUMN IF NOT EXISTS resume_compile_status TEXT
        CHECK (resume_compile_status IN ('tailored','fallback','failed'));

CREATE TABLE IF NOT EXISTS resume_compile_log (
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
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_resume_compile_log_opp
    ON resume_compile_log(opportunity_id);

-- resume_variants was reserved in V002 with only placeholder columns.
-- Add the source_kind discriminator so Phase 2 can stash 'json' variants
-- (the legacy path) alongside the new 'latex' default.
ALTER TABLE resume_variants
    ADD COLUMN IF NOT EXISTS source_kind TEXT
        CHECK (source_kind IN ('json','latex')) DEFAULT 'latex';

INSERT INTO schema_migrations(version) VALUES (7) ON CONFLICT DO NOTHING;

COMMIT;
