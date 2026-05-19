-- V017__multi_tenant_cutover.sql
-- Phase 4.2 — Multi-tenant cutover.
--
-- DROPS `DEFAULT 1` on every per-user column. The default was a Phase 1
-- hack so single-user code paths could INSERT without naming `user_id`. From
-- Phase 4.2 onwards every INSERT must explicitly carry the resolved tenant
-- via `src/common/db.current_tenant()`.
--
-- IMPORTANT: existing rows are NOT touched. All historical rows already
-- carry user_id=1 (the founding solo user) thanks to the prior default; we
-- only remove the default for *new* inserts. The `NOT NULL` constraint on
-- every user_id column is preserved — a tenant column with NULL has no
-- defined behaviour in the ranker / digest / cost ledger.
--
-- The companion `tenant_invites` table powers the `/jobs-onboard <token>`
-- slash command and the `mp tenant invite` CLI. Tokens are 64-char hex
-- (`secrets.token_hex(32)`); a row is consumed exactly once.
--
-- Rollback (manual, never via `down --volumes`):
--   ALTER TABLE <t> ALTER COLUMN user_id SET DEFAULT 1;
--   for each table listed in `_DROPPED_DEFAULT_TABLES` below.

BEGIN;

-- =========================================================================
-- 1. Drop DEFAULT 1 from every per-user column.
--
-- Tables touched (exhaustive — grep `DEFAULT 1 REFERENCES users(id)` across
-- migrations/V0*.sql to verify):
--   identities             (V001)
--   profiles               (V001)
--   applications           (V001)
--   notification_routes    (V001)
--   opportunity_scores     (V001)
--   usage_ledger           (V001)
--   resume_compile_log     (V007)
--   followups              (V009)
--   target_companies       (V010)
--   outbound_messages      (V010)
--   resume_variants        (V011)
--
-- We DO NOT touch the placeholder `user_id BIGINT` in V002 reserved tables
-- — V010/V011 already widened those into real per-user shapes covered
-- above.
-- =========================================================================

ALTER TABLE identities          ALTER COLUMN user_id DROP DEFAULT;
ALTER TABLE profiles            ALTER COLUMN user_id DROP DEFAULT;
ALTER TABLE applications        ALTER COLUMN user_id DROP DEFAULT;
ALTER TABLE notification_routes ALTER COLUMN user_id DROP DEFAULT;
ALTER TABLE opportunity_scores  ALTER COLUMN user_id DROP DEFAULT;
ALTER TABLE usage_ledger        ALTER COLUMN user_id DROP DEFAULT;
ALTER TABLE resume_compile_log  ALTER COLUMN user_id DROP DEFAULT;
ALTER TABLE followups           ALTER COLUMN user_id DROP DEFAULT;
ALTER TABLE target_companies    ALTER COLUMN user_id DROP DEFAULT;
ALTER TABLE outbound_messages   ALTER COLUMN user_id DROP DEFAULT;
ALTER TABLE resume_variants     ALTER COLUMN user_id DROP DEFAULT;

-- =========================================================================
-- 2. Tenant invites — single-use onboarding tokens.
-- =========================================================================
CREATE TABLE IF NOT EXISTS tenant_invites (
    token                CHAR(64)    PRIMARY KEY,
    created_by_user_id   BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    consumed_by_user_id  BIGINT      REFERENCES users(id) ON DELETE SET NULL,
    expires_at           TIMESTAMPTZ NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    consumed_at          TIMESTAMPTZ,
    metadata             JSONB       NOT NULL DEFAULT '{}'::jsonb
);

-- Lookup unused tokens fast (revoke / list views).
CREATE INDEX IF NOT EXISTS idx_tenant_invites_unconsumed
    ON tenant_invites (created_at DESC)
    WHERE consumed_at IS NULL;

-- Audit who joined when. Filter form keeps the index narrow.
CREATE INDEX IF NOT EXISTS idx_tenant_invites_consumed_by
    ON tenant_invites (consumed_by_user_id)
    WHERE consumed_at IS NOT NULL;

-- =========================================================================
-- 3. Augment users with multi-tenant fields the resolver needs.
-- =========================================================================
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS discord_user_id BIGINT,
    ADD COLUMN IF NOT EXISTS onboarded_via   TEXT,
    ADD COLUMN IF NOT EXISTS onboarded_at    TIMESTAMPTZ;

-- discord_user_id is the lookup key the bot uses on every interaction; it
-- must be unique per row (when set). NULL allowed — the solo `owner` row
-- (id=1) was inserted in V001 with no Discord linkage.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_users_discord_user_id
    ON users (discord_user_id)
    WHERE discord_user_id IS NOT NULL;

INSERT INTO schema_migrations (version) VALUES ('V017') ON CONFLICT DO NOTHING;

COMMIT;
