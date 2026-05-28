-- V022__auto_apply.sql
-- Auto-apply Phase 1 — schema for the policy-gated end-to-end submitter pipeline.
--
-- Three artefacts:
--   1. sources.auto_apply_enabled — per-source kill switch. Default false. Whitelist
--      a source by `UPDATE sources SET auto_apply_enabled=true WHERE slug=...`
--      AFTER its first manual submits prove the source's apply_url + extractor
--      behave. Hard rule: auto-apply NEVER fires on a source until this flips.
--   2. auto_apply_daily_count — per-user, per-date counter. Provides the hard
--      ceiling enforced by src/application/policy.py. Decoupled from
--      `daily_spend` (which tracks LLM cost in cents, not apply count) so a
--      future paid-tier user can have a different cap without table churn.
--   3. auto_apply_audit — append-only decision log. Every call to
--      policy.should_auto_submit() writes exactly one row regardless of
--      outcome (submitted / refused / dry_run / cap_exceeded / etc.). Lets
--      us reconstruct "why didn't this opp auto-apply?" from a single
--      query, which is critical during the first verification week.
--
-- Phase 1 scope: Internshala only. Other Indian platforms (Naukri, Cuvette,
-- Unstop, Contra) and US ATS land in later phases — they reuse this schema
-- unchanged. See docs/runbooks/internshala_auto_apply_dryrun.md.

BEGIN;

-- 1) Per-source kill switch ------------------------------------------------
ALTER TABLE sources
    ADD COLUMN IF NOT EXISTS auto_apply_enabled BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN sources.auto_apply_enabled IS
    'Phase 4 auto-apply kill switch. Must be flipped manually per source after '
    'the source has been verified to extract a stable apply_url + the per-method '
    'submitter has been smoke-tested on at least one dry-run. Default FALSE.';

-- 2) Per-user daily counter ------------------------------------------------
CREATE TABLE IF NOT EXISTS auto_apply_daily_count (
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    apply_date DATE NOT NULL,
    submitted_count INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, apply_date)
);

COMMENT ON TABLE auto_apply_daily_count IS
    'Per-user daily ceiling counter for auto-apply submissions. Bumped by '
    'src/application/policy.py:record_attempt only on "submit" decisions '
    '(not on refusals or dry runs). Reset implicitly by date rollover — no '
    'cron purge needed; old rows are kept for historical reference.';

-- 3) Append-only decision audit -------------------------------------------
CREATE TABLE IF NOT EXISTS auto_apply_audit (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    opportunity_id  UUID NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    application_id  BIGINT REFERENCES applications(id) ON DELETE SET NULL,
    decision        TEXT NOT NULL CHECK (decision IN (
        'submit',                  -- policy approved + dispatched to submitter
        'submit_deferred_dryrun',  -- prefs.auto_apply.dry_run=true; sidecar runs then stops
        'refused_disabled',        -- prefs.auto_apply.enabled=false
        'refused_method',          -- method not in prefs.auto_apply.methods whitelist
        'refused_source',          -- sources.auto_apply_enabled=false for opp.source_id
        'refused_score',           -- score < min_score
        'refused_no_score',        -- opportunity_scores row missing for (user, opp)
        'refused_cap',             -- auto_apply_daily_count >= max_per_day
        'refused_no_submitter'     -- no submitter registered for (method, source)
    )),
    reason          TEXT,           -- human-readable + structured (free-form for debugging)
    score           REAL,           -- snapshot of the score at decision time
    method          TEXT NOT NULL,  -- apply_method_enum text — keeps audit readable even if enum changes
    source_slug     TEXT,           -- denormalised for grep-ability
    dry_run         BOOLEAN NOT NULL DEFAULT FALSE,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE auto_apply_audit IS
    'Append-only log of every policy.should_auto_submit() call. One row per '
    'decision regardless of outcome — refusals included. Critical for '
    'debugging "why didn''t this opp auto-apply" without re-running the '
    'pipeline against stale data.';

CREATE INDEX IF NOT EXISTS auto_apply_audit_opp_idx
    ON auto_apply_audit (opportunity_id);

CREATE INDEX IF NOT EXISTS auto_apply_audit_application_idx
    ON auto_apply_audit (application_id) WHERE application_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS auto_apply_audit_user_date_idx
    ON auto_apply_audit (user_id, occurred_at DESC);

CREATE INDEX IF NOT EXISTS auto_apply_audit_decision_idx
    ON auto_apply_audit (decision, occurred_at DESC);

INSERT INTO schema_migrations (version) VALUES ('V022') ON CONFLICT DO NOTHING;

COMMIT;
