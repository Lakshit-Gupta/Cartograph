-- V021__dashboard_view_aliases.sql
-- Phase 5.2 follow-up — JS contract aliases for V019 dashboard views.
--
-- WHY THIS EXISTS
-- ---------------
-- V019 shipped `dash.v_ranker_fits`, `dash.v_source_refits`, and
-- `dash.v_recent_opps` with column names that mirror the underlying base
-- tables (`fitted_at`, `rows_used`, `ran_at`, `source_slug`). The dashboard
-- frontend in `dashboard/views/refits.js` and `dashboard/views/opps.js`
-- — which is the contract holder per CLAUDE.md "File ownership" rules —
-- reads a slightly different set of keys (`fit_at`, `version`, `n_samples`,
-- `loss`, `weights_summary`, `n_apps`, `n_sources`, `notes`, `source`).
--
-- Resolution: ADD alias projections to the three views via CREATE OR
-- REPLACE VIEW. We KEEP the original columns so any other code path that
-- already reads `fitted_at` / `rows_used` / `ran_at` / `source_slug` does
-- not break — the new aliases are pure additions to the column list.
--
-- HARD RULES BAKED IN
-- -------------------
-- 1. CREATE OR REPLACE VIEW (NOT DROP + CREATE) — preserves existing GRANTs
--    on `pgrst_anon`. ALTER DEFAULT PRIVILEGES from V019 covers any net-new
--    view, but the three views below already exist so we don't want to
--    bounce the GRANT.
-- 2. CREATE OR REPLACE VIEW in Postgres can ONLY APPEND columns to the end
--    of the existing column list — it CANNOT reorder, rename, or insert
--    mid-list. So every alias below is appended AFTER the V019 column
--    block (which is reproduced verbatim, in V019's order, at the top of
--    each SELECT). Trying to interleave new columns trips:
--       ERROR: cannot change name of view column "X" to "Y"
-- 3. The `loss = 1 - auc` formula is an APPROXIMATION (a lower-is-better
--    number derived from AUC), not a true cross-entropy loss. It is NULL
--    when `auc` is NULL (cold-start / failed fits). Surfaced for the
--    refits.js "loss" column header; a future view refactor that captures
--    real log-loss should replace this expression.
-- 4. `version` is hardcoded to the literal 'v1' — matches the
--    `ranker_version` value the ranker_worker writes into
--    `opportunity_scores`. When the ranker bumps versions, this view and
--    the worker should be updated in the same PR.
-- 5. `n_sources` aliases `weight_writes` (V012 source_refit_log) — the
--    "how many sources got new weights this run" semantic the dashboard
--    surfaces under the "sources updated" column. Same number, friendlier
--    JS key.
-- 6. `weights_summary` is a JSONB rollup of the six formula components
--    (kw_match, embedding_sim, comp_score, freshness, source_quality,
--    response_rate). NULL components stay NULL inside the JSONB so the
--    frontend can render the cold-start/failed states correctly.
--
-- FUTURE REFACTORS
-- ----------------
-- Any future view refactor (e.g. multi-tenant cutover swapping the
-- `user_id = 1` literal) MUST keep these alias columns or update the JS
-- in the same PR. The integration test
-- `tests/integration/test_dashboard_views.py::test_view_projects_frontend_columns`
-- exists specifically to catch a drift here.

BEGIN;

-- =========================================================================
-- 1. v_ranker_fits — append fit_at / version / n_samples / loss /
--    weights_summary at the END of the V019 column list (CREATE OR REPLACE
--    cannot reorder, only extend — see HARD RULE 2 above).
-- =========================================================================

CREATE OR REPLACE VIEW dash.v_ranker_fits AS
SELECT
    -- ── V019 columns, IN V019 ORDER (do not reshuffle — see HARD RULE 2). ──
    id,
    fitted_at,
    status,
    rows_used,
    positive_rate,
    auc,
    kw_match,
    embedding_sim,
    comp_score,
    freshness,
    source_quality,
    response_rate,

    -- ── V021 alias columns, appended at the end. ──

    -- JS contract alias: dashboard/views/refits.js reads `fit_at`.
    fitted_at                                            AS fit_at,

    -- Hardcoded ranker version literal. Matches the 'v1' value that
    -- ranker_worker stamps into opportunity_scores.ranker_version. When
    -- the ranker bumps versions, update this literal AND ranker_worker
    -- in the same PR.
    'v1'::text                                           AS version,

    -- JS contract alias: dashboard/views/refits.js reads `n_samples`.
    rows_used                                            AS n_samples,

    -- "loss" surfaced as an approximate (1 - AUC) — NOT a true cross-entropy
    -- loss. NULL when auc is NULL (cold-start / failed fits). A future
    -- ranker_weights_fit schema extension could write a real log-loss
    -- column; until then this expression gives the frontend a lower-is-
    -- better number for the "loss" column header.
    CASE WHEN auc IS NULL THEN NULL
         ELSE (1.0 - auc)::real END                      AS loss,

    -- JSONB rollup of the six formula components for the "weights" column
    -- in the refits panel. Nulls inside stay nulls so cold-start / failed
    -- rows still render as expected (the JS stringifies the object).
    jsonb_build_object(
        'kw_match',       kw_match,
        'embedding_sim',  embedding_sim,
        'comp_score',     comp_score,
        'freshness',      freshness,
        'source_quality', source_quality,
        'response_rate',  response_rate
    )                                                    AS weights_summary
FROM ranker_weights_fit
WHERE user_id = 1
ORDER BY fitted_at DESC
LIMIT 14;

COMMENT ON VIEW dash.v_ranker_fits IS
'Last 14 global ranker weight fits. Projects both fitted_at/rows_used (base) '
'and fit_at/n_samples/version/loss/weights_summary (JS contract). loss is an '
'approximation (1 - auc), NOT true log-loss.';

-- =========================================================================
-- 2. v_source_refits — append fit_at / n_apps / n_sources / notes after
--    the V019 column list.
-- =========================================================================

CREATE OR REPLACE VIEW dash.v_source_refits AS
SELECT
    -- ── V019 columns, IN V019 ORDER. ──
    id,
    ran_at,
    status,
    rows_used,
    positive_rate,
    auc,
    weight_writes,

    -- ── V021 alias columns, appended at the end. ──

    -- JS contract alias: dashboard/views/refits.js reads `fit_at`.
    ran_at                                               AS fit_at,

    -- JS contract alias: dashboard/views/refits.js reads `n_apps`.
    rows_used                                            AS n_apps,

    -- JS contract alias: dashboard/views/refits.js reads `n_sources`
    -- (semantically: "how many sources got new weights this run").
    weight_writes                                        AS n_sources,

    -- "notes" — human-readable status string. On status='ok' we encode
    -- the two headline numbers (auc + positive_rate) the operator wants
    -- at a glance; on every other status we surface the raw status so
    -- cold_start / failed is visible without a separate column.
    CASE
        WHEN status = 'ok' THEN
            format(
                'auc=%s, pos=%s',
                COALESCE(round(auc::numeric, 3)::text, 'NA'),
                round(positive_rate::numeric, 3)::text
            )
        ELSE status
    END                                                  AS notes
FROM source_refit_log
ORDER BY ran_at DESC
LIMIT 14;

COMMENT ON VIEW dash.v_source_refits IS
'Last 14 weekly source-refit runs (audit ledger, no user scope). Projects both '
'ran_at/rows_used/weight_writes (base) and fit_at/n_apps/n_sources/notes '
'(JS contract).';

-- =========================================================================
-- 3. v_recent_opps — append `source` alias after the V019 column list.
-- =========================================================================

CREATE OR REPLACE VIEW dash.v_recent_opps AS
SELECT
    -- ── V019 columns, IN V019 ORDER. ──
    o.id                                                 AS opportunity_id,
    o.title,
    o.company,
    o.category,
    o.state,
    o.posted_at,
    o.first_seen,
    o.apply_url,
    s.slug                                               AS source_slug,
    s.name                                               AS source_name,
    sc.score,
    sc.score_components,
    sc.scored_at,

    -- ── V021 alias column, appended at the end. ──

    -- JS contract alias: dashboard/views/opps.js sorts AND displays under
    -- the key `source` (the COLS array in opps.js carries `key: "source"`).
    -- Keep `source_slug` too — existing code paths read that name.
    s.slug                                               AS source
FROM opportunities o
JOIN sources s ON s.id = o.source_id
LEFT JOIN opportunity_scores sc
    ON sc.opportunity_id = o.id AND sc.user_id = 1
ORDER BY o.first_seen DESC
LIMIT 50;

COMMENT ON VIEW dash.v_recent_opps IS
'Last 50 opportunities (any state). Score is owner-scoped; NULL for unscored '
'opps. Projects both source_slug (base) and source (JS contract).';

-- =========================================================================
-- 4. Schema-migrations marker (own-row contract per CLAUDE.md).
--
-- CREATE OR REPLACE VIEW preserves the V019 GRANTs on pgrst_anon, so no
-- re-GRANT statement is required here. ALTER DEFAULT PRIVILEGES from V019
-- still applies to any future net-new dash.v_* view.
-- =========================================================================

INSERT INTO schema_migrations (version) VALUES ('V021') ON CONFLICT DO NOTHING;

COMMIT;
