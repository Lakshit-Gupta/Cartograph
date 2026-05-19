-- V012__source_refit_log.sql
-- Phase 2.4 — Source response-rate feedback audit log.
--
-- The weekly cron in src/ranker/source_refit.py fits a logistic regression
-- over the last 90 days of applications + opportunity_transitions, then
-- writes per-source weights back into sources.ranking_weight (which the
-- ranker formula already consumes via formula.source_quality). Every run
-- — successful, cold-start, or failed — appends one row here for audit.
--
-- Why an audit table instead of just structured logs:
--   - The Pi-side log volume is high; structured logs roll over weekly.
--     This audit lives forever and is small (<= 52 rows/year/user).
--   - /status and the costs dashboard surface "last refit" + AUC trend.
--   - Cold-start gating (rows_used < 50) is debugged by inspecting the
--     row history rather than by replaying a cron.
--
-- Idempotence: the cron is deterministic (random_state pinned), but each
-- INVOCATION should produce one row regardless — re-running on the same
-- data is fine (sources.ranking_weight gets the same value twice and the
-- second insert here gets a fresh timestamp).
--
-- Hard rules:
--   1. No FK to sources or users — this is an audit ledger, must survive
--      source deletion (sources rarely get deleted, but if a source is
--      removed we still want the history of its weight).
--   2. coefficient_summary is JSONB { source_id: {coef, weight}, ... }
--      so the read path can render a per-source diff. Capped at ~1KB per
--      row because we never expect more than ~30 active sources.
--   3. status check matches the three terminal states the cron emits.

BEGIN;

CREATE TABLE IF NOT EXISTS source_refit_log (
    id                  BIGSERIAL PRIMARY KEY,
    ran_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    rows_used           INTEGER NOT NULL,
    positive_rate       REAL NOT NULL,
    auc                 REAL,
    coefficient_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    weight_writes       INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL
                          CHECK (status IN ('ok','cold_start','failed')),
    error_message       TEXT
);

CREATE INDEX IF NOT EXISTS idx_source_refit_log_ran_at
    ON source_refit_log (ran_at DESC);

INSERT INTO schema_migrations (version) VALUES ('V012') ON CONFLICT DO NOTHING;

COMMIT;
