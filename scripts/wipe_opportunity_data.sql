-- ============================================================================
-- wipe_opportunity_data.sql
-- ----------------------------------------------------------------------------
-- Purpose:
--   Nuke every row of opportunity-derived data so the pipeline re-ingests
--   from a clean slate. Preserves schema, users, identities, profiles, the
--   source seed rows (slug/config), schema_migrations, the opp state machine
--   table (opp_state_transitions_allowed), and ranker fit history
--   (ranker_weights_fit).
--
-- FK graph (verified against V001/V007/V009/V010/V022/V023/V024):
--   opportunities (PK uuid)
--     ├── opportunity_scores         (FK opp_id ON DELETE CASCADE)
--     ├── opportunity_transitions    (FK opp_id ON DELETE CASCADE)
--     ├── applications               (FK opp_id ON DELETE CASCADE)
--     │     ├── followups            (FK application_id ON DELETE CASCADE)
--     │     └── auto_apply_audit.application_id (ON DELETE SET NULL)
--     ├── auto_apply_audit           (FK opp_id ON DELETE CASCADE)
--     └── resume_compile_log         (FK opp_id ON DELETE CASCADE)
--
--   auto_apply_daily_count           — no FK to opp; user/source/persona keyed.
--   outbound_messages                — NO FK to opportunities (cold-email
--                                       ledger keyed off target_companies);
--                                       included per user directive to reset.
--
-- Strategy:
--   Single transaction. TRUNCATE ... RESTART IDENTITY CASCADE on the leaves
--   first (FK-safe), then TRUNCATE opportunities. RESTART IDENTITY resets
--   BIGSERIAL sequences so IDs start at 1 again. We do NOT use CASCADE on
--   `opportunities` blindly — leaves are listed explicitly so the script
--   self-documents what it nukes (defensive against a future migration
--   adding a new FK that should NOT be wiped).
--
-- Idempotent: every TRUNCATE is gated on table existence via to_regclass.
-- Re-runnable safely; second run truncates already-empty tables (no-op).
--
-- Authored 2026-05-29.
-- ============================================================================

BEGIN;

-- Hold an advisory lock so a concurrent runner can't race the wipe.
-- 727275 = arbitrary distinct from the migration lock (727274).
SELECT pg_advisory_xact_lock(727275);

-- ----------------------------------------------------------------------------
-- 1. Leaf tables (children of applications + opportunities).
--    Order: followups (FK→applications) BEFORE applications.
--           auto_apply_audit (FK→applications + FK→opportunities) BEFORE both.
-- ----------------------------------------------------------------------------

DO $$
BEGIN
    IF to_regclass('public.followups') IS NOT NULL THEN
        TRUNCATE TABLE followups RESTART IDENTITY CASCADE;
    END IF;

    IF to_regclass('public.auto_apply_audit') IS NOT NULL THEN
        TRUNCATE TABLE auto_apply_audit RESTART IDENTITY CASCADE;
    END IF;

    IF to_regclass('public.auto_apply_daily_count') IS NOT NULL THEN
        TRUNCATE TABLE auto_apply_daily_count;  -- composite PK, no serial
    END IF;

    IF to_regclass('public.resume_compile_log') IS NOT NULL THEN
        TRUNCATE TABLE resume_compile_log RESTART IDENTITY CASCADE;
    END IF;

    IF to_regclass('public.outbound_messages') IS NOT NULL THEN
        TRUNCATE TABLE outbound_messages RESTART IDENTITY CASCADE;
    END IF;
END $$;

-- ----------------------------------------------------------------------------
-- 2. Mid-tier (children of opportunities).
-- ----------------------------------------------------------------------------

DO $$
BEGIN
    IF to_regclass('public.applications') IS NOT NULL THEN
        TRUNCATE TABLE applications RESTART IDENTITY CASCADE;
    END IF;

    IF to_regclass('public.opportunity_scores') IS NOT NULL THEN
        TRUNCATE TABLE opportunity_scores;  -- composite PK, no serial
    END IF;

    IF to_regclass('public.opportunity_transitions') IS NOT NULL THEN
        TRUNCATE TABLE opportunity_transitions RESTART IDENTITY CASCADE;
    END IF;
END $$;

-- ----------------------------------------------------------------------------
-- 3. opportunities root. CASCADE catches any FK-children added later that
--    aren't yet in this script (defense-in-depth; expected to be a no-op).
-- ----------------------------------------------------------------------------

TRUNCATE TABLE opportunities RESTART IDENTITY CASCADE;

-- ----------------------------------------------------------------------------
-- 4. Reset sources.last_successful_crawl_at so scheduler immediately
--    re-emits fetch tasks on the next tick. Preserves slug, config,
--    auth_account_id, auto_apply_enabled, ranking_weight, etc.
-- ----------------------------------------------------------------------------

UPDATE sources
   SET last_successful_crawl_at = NULL,
       opps_extracted_30d        = 0;

-- ----------------------------------------------------------------------------
-- 5. Final SELECT — row counts of every touched table. All should be 0
--    except `sources` (preserved, count unchanged).
-- ----------------------------------------------------------------------------

SELECT 'opportunities'             AS table_name, COUNT(*) AS row_count FROM opportunities
UNION ALL
SELECT 'opportunity_scores',                         COUNT(*) FROM opportunity_scores
UNION ALL
SELECT 'opportunity_transitions',                    COUNT(*) FROM opportunity_transitions
UNION ALL
SELECT 'applications',                               COUNT(*) FROM applications
UNION ALL
SELECT 'auto_apply_audit',                           COUNT(*) FROM auto_apply_audit
UNION ALL
SELECT 'auto_apply_daily_count',                     COUNT(*) FROM auto_apply_daily_count
UNION ALL
SELECT 'resume_compile_log',                         COUNT(*) FROM resume_compile_log
UNION ALL
SELECT 'followups',                                  COUNT(*) FROM followups
UNION ALL
SELECT 'outbound_messages',                          COUNT(*) FROM outbound_messages
UNION ALL
SELECT 'sources (preserved)',                        COUNT(*) FROM sources
UNION ALL
SELECT 'sources with last_successful_crawl_at NULL', COUNT(*) FROM sources
        WHERE last_successful_crawl_at IS NULL
ORDER BY table_name;

COMMIT;

-- ============================================================================
-- End of wipe_opportunity_data.sql
-- ============================================================================
