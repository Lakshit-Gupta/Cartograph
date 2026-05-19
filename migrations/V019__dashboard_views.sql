-- V019__dashboard_views.sql
-- Phase 5.2 — Web dashboard backend (PostgREST + read-only views).
--
-- Stands up a `dash` schema of READ-ONLY views the dashboard frontend hits
-- via PostgREST (Tailscale-only, never published to the public internet).
-- A dedicated `pgrst_anon` Postgres role owns NOTHING and has SELECT on the
-- `dash` schema only — defense-in-depth so a PostgREST RCE cannot escape
-- into application tables.
--
-- Why owner-scoping is hardcoded to `user_id = 1`:
--   - Phase 5.2 ships dashboard for the SOLO deployment (single owner,
--     Tailscale-only). No JWT issuance, no multi-tenant request routing.
--   - V017 already dropped DEFAULT 1 on per-user columns; the solo owner
--     row at users.id=1 (V001 seed) remains the only populated tenant.
--   - When Phase 4.2+ flips multi-tenant on, V0XX will swap the literal
--     `1` for `(current_setting('request.jwt.claims', true)::json ->> 'user_id')::bigint`
--     and add a `pgrst_auth` JWT-issuing role. Until then the simpler path
--     (a) keeps the JWT-secret blast radius to zero,
--     (b) makes the dashboard fully usable on day one with no auth UX,
--     (c) leaves an obvious grep target (`user_id = 1`) for the cutover PR.
--
-- Hard rules baked in:
--   1. Every view is read-only (CREATE VIEW, never CREATE TABLE).
--   2. The pgrst_anon role gets SELECT on dash.* only — never on raw
--      application tables. PostgREST cannot reach `identities`,
--      `encrypted_credentials`, etc.
--   3. ALTER DEFAULT PRIVILEGES … GRANT SELECT means future views drop in
--      automatically; no follow-up GRANT needed when V020+ adds more.
--   4. The views NEVER expose: encrypted bytes, raw resumes, JWT secrets,
--      identity vault rows, cookie cache. The frontend only sees aggregated
--      stats + opportunity titles + application outcomes.
--   5. NO indexes on views (Postgres won't index views anyway; the
--      underlying base-table indexes already cover the hot paths).

BEGIN;

-- =========================================================================
-- 1. Schema + role
-- =========================================================================

CREATE SCHEMA IF NOT EXISTS dash;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pgrst_anon') THEN
        CREATE ROLE pgrst_anon NOLOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA dash TO pgrst_anon;

-- =========================================================================
-- 2. v_overview — single-row dashboard headline tiles.
--
-- Every counter is owner-scoped (user_id = 1) where the underlying table
-- has a user_id column. Source / opportunity counters are global (sources
-- and opportunities are not per-user — they get scored per-user in
-- opportunity_scores).
-- =========================================================================

CREATE OR REPLACE VIEW dash.v_overview AS
SELECT
    -- Opps seen in last 24h.
    (SELECT COUNT(*)::INT FROM opportunities
        WHERE first_seen >= NOW() - INTERVAL '24 hours')
        AS opps_24h,

    -- Applications created today (UTC date — matches the cost ledger
    -- below; the frontend can render in the user's TZ if needed).
    (SELECT COUNT(*)::INT FROM applications
        WHERE user_id = 1 AND sent_at::date = CURRENT_DATE)
        AS applied_today,

    -- Applications sent in last 24h — distinct from applied_today (which
    -- is calendar-date-bound).
    (SELECT COUNT(*)::INT FROM applications
        WHERE user_id = 1 AND sent_at >= NOW() - INTERVAL '24 hours')
        AS sent_24h,

    -- Applications sent in last 7 days.
    (SELECT COUNT(*)::INT FROM applications
        WHERE user_id = 1 AND sent_at >= NOW() - INTERVAL '7 days')
        AS applied_7d,

    -- Response rate over last 30 days (responded ÷ sent). NULL when no
    -- applications in the window — the frontend should render that as
    -- "—" rather than 0.
    (SELECT CASE WHEN COUNT(*) = 0 THEN NULL
                 ELSE COUNT(*) FILTER (WHERE response_status IS NOT NULL)::REAL / COUNT(*)::REAL
            END
        FROM applications
        WHERE user_id = 1 AND sent_at >= NOW() - INTERVAL '30 days')
        AS response_rate_30d,

    -- Spend today + month-to-date, in USD. usage_ledger.cost_usd_micros
    -- is fixed-point (1 USD = 1_000_000) — divide once here so the
    -- frontend doesn't need to know the encoding.
    (SELECT COALESCE(SUM(cost_usd_micros), 0)::BIGINT / 1000000.0
        FROM usage_ledger
        WHERE user_id = 1 AND ts::date = CURRENT_DATE)
        AS cost_today_usd,

    (SELECT COALESCE(SUM(cost_usd_micros), 0)::BIGINT / 1000000.0
        FROM usage_ledger
        WHERE user_id = 1 AND ts >= date_trunc('month', CURRENT_DATE))
        AS cost_mtd_usd,

    -- Source health (global, not per-user).
    (SELECT COUNT(*)::INT FROM sources WHERE status = 'active')
        AS active_sources,
    (SELECT COUNT(*)::INT FROM sources WHERE status = 'quarantined')
        AS quarantined_sources,

    -- Identity health (global — identities are per-user in V001, but the
    -- aggregate health view here is over user_id = 1 to stay consistent
    -- with the rest of the row).
    (SELECT COUNT(*)::INT FROM identities
        WHERE user_id = 1 AND ban_status = 'healthy')
        AS healthy_identities;

COMMENT ON VIEW dash.v_overview IS
'Single-row dashboard headline tile. Owner-scoped to user_id=1 (solo deployment).';

-- =========================================================================
-- 3. v_recent_opps — last 50 ranked/digested opportunities.
--
-- Join through opportunity_scores so we surface the user's score + the
-- breakdown JSONB the ranker writes (kw_match, embedding_sim, …). LEFT
-- JOIN because opps in state='new' or 'queued' have no score row yet —
-- they should still appear with NULL score so the operator sees them.
-- =========================================================================

CREATE OR REPLACE VIEW dash.v_recent_opps AS
SELECT
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
    sc.scored_at
FROM opportunities o
JOIN sources s ON s.id = o.source_id
LEFT JOIN opportunity_scores sc
    ON sc.opportunity_id = o.id AND sc.user_id = 1
ORDER BY o.first_seen DESC
LIMIT 50;

COMMENT ON VIEW dash.v_recent_opps IS
'Last 50 opportunities (any state). Score is owner-scoped; NULL for unscored opps.';

-- =========================================================================
-- 4. v_recent_applications — last 100 applications with outcome data.
-- =========================================================================

CREATE OR REPLACE VIEW dash.v_recent_applications AS
SELECT
    a.id                                                 AS application_id,
    a.opportunity_id,
    o.title,
    o.company,
    a.method,
    a.sent_at,
    a.response_status,
    a.response_at,
    a.resume_compile_status
FROM applications a
JOIN opportunities o ON o.id = a.opportunity_id
WHERE a.user_id = 1
ORDER BY a.sent_at DESC
LIMIT 100;

COMMENT ON VIEW dash.v_recent_applications IS
'Last 100 applications for user_id=1 with response outcome + resume compile status.';

-- =========================================================================
-- 5. v_cost_daily — last 30 days of cost rollup from usage_ledger.
--
-- Aggregated per (date, kind, model) so a single day with many small calls
-- collapses into a single row per model. The frontend renders this as a
-- stacked bar chart.
-- =========================================================================

CREATE OR REPLACE VIEW dash.v_cost_daily AS
SELECT
    ts::date                                             AS date,
    kind,
    model,
    SUM(cost_usd_micros)::BIGINT / 1000000.0             AS usd,
    SUM(input_tokens)::BIGINT                            AS input_tokens,
    SUM(output_tokens)::BIGINT                           AS output_tokens,
    COUNT(*)::INT                                        AS call_count
FROM usage_ledger
WHERE user_id = 1
  AND ts >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY ts::date, kind, model
ORDER BY ts::date DESC, kind, model;

COMMENT ON VIEW dash.v_cost_daily IS
'30-day usage ledger rollup, grouped by (date, kind, model).';

-- =========================================================================
-- 6. v_source_health — every source with health + extraction stats.
-- =========================================================================

CREATE OR REPLACE VIEW dash.v_source_health AS
SELECT
    s.slug,
    s.name,
    s.category,
    s.status,
    s.priority,
    s.opps_extracted_30d,
    s.last_successful_crawl_at,
    s.ban_observed_at,
    s.last_cf_challenge_at,
    s.ranking_weight,
    s.cf_protection_level
FROM sources s
ORDER BY s.status, s.priority DESC, s.slug;

COMMENT ON VIEW dash.v_source_health IS
'Per-source health: status, opps_extracted_30d, ranking weight, CF protection level.';

-- =========================================================================
-- 7. v_ranker_fits — last 14 rows from ranker_weights_fit.
--
-- Exposes the six formula components so the frontend can render a
-- stacked-bar "weights over time" chart. raw_coefficients JSONB is
-- intentionally NOT exposed — it is a debug artifact, not a user-facing
-- field. error_message also stays internal (status=='failed' rows show
-- with NULL weights and that's all the dashboard needs).
-- =========================================================================

CREATE OR REPLACE VIEW dash.v_ranker_fits AS
SELECT
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
    response_rate
FROM ranker_weights_fit
WHERE user_id = 1
ORDER BY fitted_at DESC
LIMIT 14;

COMMENT ON VIEW dash.v_ranker_fits IS
'Last 14 global ranker weight fits. Surfaces status + the six formula components.';

-- =========================================================================
-- 8. v_source_refits — last 14 source-refit audit rows.
--
-- source_refit_log has no user_id (V012 — it's an audit ledger surviving
-- source deletion), so this view is global. The dashboard shows trends.
-- =========================================================================

CREATE OR REPLACE VIEW dash.v_source_refits AS
SELECT
    id,
    ran_at,
    status,
    rows_used,
    positive_rate,
    auc,
    weight_writes
FROM source_refit_log
ORDER BY ran_at DESC
LIMIT 14;

COMMENT ON VIEW dash.v_source_refits IS
'Last 14 weekly source-refit runs (audit ledger, no user scope).';

-- =========================================================================
-- 9. Grants — pgrst_anon SELECT on every dash.* view (now and future).
-- =========================================================================

GRANT SELECT ON ALL TABLES IN SCHEMA dash TO pgrst_anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA dash GRANT SELECT ON TABLES TO pgrst_anon;

INSERT INTO schema_migrations (version) VALUES ('V019') ON CONFLICT DO NOTHING;

COMMIT;
