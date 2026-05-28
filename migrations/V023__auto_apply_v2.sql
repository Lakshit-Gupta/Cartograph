-- V023__auto_apply_v2.sql
-- Two unrelated fixes batched because both unblock Phase 4 auto-apply v2:
--
--   1. Allow `ranked → applied` (+ siblings) so `/apply` clicked from
--      Discord stops bouncing on the state-machine trigger. Today the only
--      legal "go to applied" paths are from `digested`, `seen`, `snoozed` —
--      but most opps the user actually wants to apply to sit at `ranked`
--      (digest hasn't fired yet) or `queued` (just crawled).
--
--   2. Add `opportunities.comp_min_inr` for currency-normalized comp.
--      Crawlers store native currency in `comp_min` + `comp_currency`; the
--      auto-apply filter needs a single comparable INR figure for opps
--      that mix INR-stipend internships, USD-paid contract roles, and
--      GBP fellowships. We compute INR-equivalent during ranker_worker
--      (existing rate table at src/common/currency.py) and persist here
--      so the policy SQL filter (in src/application/auto_apply_engine.py)
--      can `WHERE comp_min_inr >= filter_min` without per-row Python.

BEGIN;

-- 1) Add the four missing state transitions ---------------------------------
INSERT INTO opp_state_transitions_allowed (from_state, to_state) VALUES
    ('queued',  'applied'),
    ('ranked',  'applied'),
    ('new',     'applied'),
    ('queued',  'ranked'),     -- defensive: was missing; some opps go queued→ranked
    ('queued',  'digested')    -- digest cron may pick a queued opp directly
ON CONFLICT DO NOTHING;

-- 2) INR-normalized comp column --------------------------------------------
ALTER TABLE opportunities
    ADD COLUMN IF NOT EXISTS comp_min_inr REAL;

COMMENT ON COLUMN opportunities.comp_min_inr IS
    'comp_min normalized to INR using a snapshot rate table '
    '(src/common/currency.py). NULL when comp_min itself is NULL OR the '
    'currency is unrecognised. Populated by ranker_worker at score time; '
    'the auto-apply filter in src/application/auto_apply_engine.py reads '
    'this column rather than re-running currency conversion per request.';

CREATE INDEX IF NOT EXISTS opp_comp_min_inr_idx
    ON opportunities (comp_min_inr)
    WHERE comp_min_inr IS NOT NULL;

INSERT INTO schema_migrations (version) VALUES ('V023') ON CONFLICT DO NOTHING;

COMMIT;
