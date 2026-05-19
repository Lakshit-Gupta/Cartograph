-- V009__followups.sql
-- Phase 2.3 — follow-up automation. Daily 13:00 IST cron scans applications
-- aged >= window_days with no recorded inbound reply and no prior followup,
-- drafts an 80-word follow-up via the LLM writer model, then surfaces it
-- in Discord with Send / Edit / Skip buttons.
--
-- Multi-tenant ready: user_id NOT NULL DEFAULT 1 lands the same shape we
-- already use across applications, opportunity_scores, resume_compile_log.
--
-- Why ALTER + CREATE: src/application/sender.py ships a runtime DDL
-- (CREATE TABLE IF NOT EXISTS followups) that uses a *different* shape
-- (fire_at / fired_at, no body / status). That runtime DDL was the Phase 1
-- placeholder for queue_followup; this migration brings the table up to
-- the Phase 2.3 shape WITHOUT dropping the existing rows. The ALTERs are
-- IF NOT EXISTS so this migration is idempotent against either a brand-new
-- DB (the CREATE TABLE IF NOT EXISTS below claims first) or a DB that
-- already has the legacy two-column shape (the ALTERs claim).

BEGIN;

CREATE TABLE IF NOT EXISTS followups (
    id              BIGSERIAL PRIMARY KEY,
    application_id  BIGINT NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    fire_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    fired_at        TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Bring up to Phase 2.3 shape.
ALTER TABLE followups
    ADD COLUMN IF NOT EXISTS user_id            BIGINT NOT NULL DEFAULT 1 REFERENCES users(id),
    ADD COLUMN IF NOT EXISTS scheduled_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS sent_at            TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS body_markdown      TEXT,
    ADD COLUMN IF NOT EXISTS status             TEXT NOT NULL DEFAULT 'draft',
    ADD COLUMN IF NOT EXISTS resend_message_id  TEXT;

-- CHECK constraint is added separately so the migration tolerates a
-- pre-existing followups row with a status value outside the new set
-- (there shouldn't be any in solo phase, but Phase 4 friend onboarding
-- shouldn't trip on a partial rollout). DO block makes it idempotent.
DO $$ BEGIN
    ALTER TABLE followups
        ADD CONSTRAINT followups_status_check
        CHECK (status IN ('draft','sent','edited','skipped','failed'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Idempotent UNIQUE — one followup per application. Cron is then safe to
-- run twice in the same day; second run hits ON CONFLICT and no-ops.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_followups_application
    ON followups (application_id);

CREATE INDEX IF NOT EXISTS idx_followups_user_status_scheduled
    ON followups (user_id, status, scheduled_at);

INSERT INTO schema_migrations (version) VALUES ('V009') ON CONFLICT DO NOTHING;

COMMIT;
