-- V010__outbound_messages.sql
-- Phase 2.1 — Cold email outbound lane.
--
-- Two new tables backing the cold-outreach funnel:
--   target_companies — user-curated list of companies to cold-email into.
--                      V002 reserved this name with a placeholder column;
--                      this migration evolves it to the real shape.
--   outbound_messages — one row per cold email actually sent. Kept SEPARATE
--                      from `applications` so response-rate metrics on the
--                      reply-to-listing path stay clean (CLAUDE.md "Phase 2"
--                      mandate).
--
-- Hard rules:
--   1. Subject-per-day uniqueness is enforced in code (cap.py) because
--      `sent_at::date` is STABLE-not-IMMUTABLE under PG and would fail the
--      pre-commit migrate-replay hook ("functions in index expression must
--      be marked IMMUTABLE"). We keep an idx on (subject_hash, sent_at) so
--      the daily-window scan is cheap; cap.py raises before INSERT if a
--      collision is detected in the last 30 days.
--   2. No same-recipient cold email within 14 days — same rationale; lookup
--      lives in cap.py against idx_outbound_recipient_user.
--   3. `user_id NOT NULL DEFAULT 1` for Phase 4 multi-tenant readiness.
-- ---------------------------------------------------------------

BEGIN;

-- --- target_companies: evolve the V002 placeholder. -----------------------
-- V002 created the table with a single `placeholder TEXT` column. We add
-- the real columns idempotently, then drop the placeholder once present.

ALTER TABLE target_companies
    ADD COLUMN IF NOT EXISTS user_id BIGINT NOT NULL DEFAULT 1 REFERENCES users(id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS name TEXT,
    ADD COLUMN IF NOT EXISTS domain TEXT,
    ADD COLUMN IF NOT EXISTS mission_summary TEXT,
    ADD COLUMN IF NOT EXISTS why_target TEXT,
    ADD COLUMN IF NOT EXISTS added_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- Drop placeholder if it still exists.
ALTER TABLE target_companies DROP COLUMN IF EXISTS placeholder;

-- name must be NOT NULL now; only enforce after the column exists.
UPDATE target_companies SET name = COALESCE(name, 'unknown-' || id::text) WHERE name IS NULL;
ALTER TABLE target_companies ALTER COLUMN name SET NOT NULL;

-- One (user, domain) pair per row when domain is set. NULL domains allowed
-- (user-typed companies before discovery). lower() is IMMUTABLE so this
-- expression index is safe.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_target_company
    ON target_companies (user_id, lower(domain))
    WHERE domain IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_target_companies_user
    ON target_companies(user_id);

-- --- outbound_messages: the cold-email ledger. ----------------------------
CREATE TABLE IF NOT EXISTS outbound_messages (
    id                  BIGSERIAL PRIMARY KEY,
    user_id             BIGINT NOT NULL DEFAULT 1 REFERENCES users(id) ON DELETE CASCADE,
    target_company_id   BIGINT REFERENCES target_companies(id) ON DELETE SET NULL,
    recipient_email     TEXT NOT NULL,
    recipient_name      TEXT,
    subject             TEXT NOT NULL,
    subject_hash        CHAR(64) NOT NULL,
    body_markdown       TEXT NOT NULL,
    sent_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    response_status     TEXT NOT NULL DEFAULT 'pending'
                          CHECK (response_status IN
                            ('pending','replied','rejected','bounce','unsubscribe','no_response')),
    response_at         TIMESTAMPTZ,
    thread_id           TEXT,
    resend_message_id   TEXT
);

CREATE INDEX IF NOT EXISTS idx_outbound_user_sent
    ON outbound_messages(user_id, sent_at DESC);

-- Subject-hash + sent_at: the cap module probes
--   WHERE subject_hash = $1 AND sent_at >= NOW() - INTERVAL '30 days'
-- before every send. This composite index makes that probe an index-only
-- scan. (We can't use a unique expression index on sent_at::date because
-- it's STABLE-not-IMMUTABLE under PG semantics.)
CREATE INDEX IF NOT EXISTS idx_outbound_subject_hash_sent
    ON outbound_messages(subject_hash, sent_at DESC);

CREATE INDEX IF NOT EXISTS idx_outbound_recipient_user
    ON outbound_messages(user_id, lower(recipient_email), sent_at DESC);

-- Thread-id lookup used by the Gmail watcher to attribute replies back
-- to outbound_messages (vs applications). Header chain: outbound row
-- writes its Message-ID into thread_id; gmail_watcher matches by
-- In-Reply-To / References.
CREATE INDEX IF NOT EXISTS idx_outbound_thread_id
    ON outbound_messages(thread_id)
    WHERE thread_id IS NOT NULL;

INSERT INTO schema_migrations (version) VALUES ('V010') ON CONFLICT DO NOTHING;

COMMIT;
