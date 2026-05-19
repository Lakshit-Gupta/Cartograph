-- V018__ranker_weights_fit.sql
-- Phase 5.3 — advanced ranker (response-rate-weighted ML refit nightly).
--
-- Persists fitted global ranker weights (the six components in
-- src.ranker.formula.RankerWeights: kw_match, embedding_sim, comp_score,
-- freshness, source_quality, response_rate). Each nightly cron run inserts
-- one row; the formula ranker reads the most-recent ok-status row on every
-- score() call (with a process-cache to avoid hot-pathing the DB).
--
-- Why a fresh table rather than augmenting the YAML / source_refit_log?
--   1. source_refit_log audits *per-source* weights; this is *global*
--      formula weights — a different feature space, different cardinality.
--   2. Versioned audit: cold-start, failures, and successful fits all live
--      side-by-side so a regression is obvious in `SELECT ... ORDER BY id DESC`.
--   3. Latest-row read pattern stays cheap: one indexed scan keyed on
--      `fitted_at` (or the trivial single-row tail of a status='ok' filter).
--
-- Free-only constraint: pure sklearn local fit. No LLM, no proxy, no spend.
-- Cold-start gate at ranker module level: <50 labeled apps → status='cold_start',
-- weights left null, formula falls back to YAML defaults — identical to the
-- Phase 2.4 source_refit pattern.

BEGIN;

CREATE TABLE IF NOT EXISTS ranker_weights_fit (
    id                  BIGSERIAL PRIMARY KEY,
    user_id             BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    fitted_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Final mapped weights — NULL for cold_start / failed runs. The six
    -- ranker formula components, each in [0.0, 1.0] and summing to ~1.0
    -- (we min-max scale + L1-normalise after fit to keep them comparable
    -- to the YAML defaults).
    kw_match            REAL,
    embedding_sim       REAL,
    comp_score          REAL,
    freshness           REAL,
    source_quality      REAL,
    response_rate       REAL,

    -- Audit columns.
    rows_used           INT NOT NULL,
    positive_rate       REAL NOT NULL,
    auc                 REAL,          -- NULL when y is single-class
    raw_coefficients    JSONB NOT NULL DEFAULT '{}'::jsonb,
    status              TEXT NOT NULL CHECK (status IN ('ok', 'cold_start', 'failed')),
    error_message       TEXT
);

-- "Latest successful fit per tenant" is the only hot query. The DESC index
-- on (user_id, fitted_at) lets the ranker grab it with a one-row index scan.
CREATE INDEX IF NOT EXISTS idx_ranker_weights_fit_latest_ok
    ON ranker_weights_fit (user_id, fitted_at DESC)
    WHERE status = 'ok';

-- Audit-side index — operators tracking failed runs week-over-week.
CREATE INDEX IF NOT EXISTS idx_ranker_weights_fit_status
    ON ranker_weights_fit (status, fitted_at DESC)
    WHERE status <> 'ok';

INSERT INTO schema_migrations (version) VALUES ('V018') ON CONFLICT DO NOTHING;

COMMIT;
