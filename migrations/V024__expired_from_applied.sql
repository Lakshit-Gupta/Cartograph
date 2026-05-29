-- V024__expired_from_applied.sql
-- Allow `applied -> expired` so apply-result-worker can mark closed
-- Internshala listings as expired without leaving them stuck at `applied`.
--
-- Context (2026-05-29): the auto-apply flow transitions opp to `applied`
-- BEFORE the sidecar fires (so the apply-result-worker has a target row
-- to UPDATE). When the sidecar discovers the listing is closed
-- ("Applications are closed for this internship" banner), it returns
-- status='closed'. apply-result-worker needs to roll the opp out of
-- `applied` (we didn't really apply) and into `expired` (terminal state,
-- cron never re-fires). Without the transition pair below the state
-- machine trigger refuses the UPDATE.
--
-- Sibling transitions (`digested -> expired`, `seen -> expired`,
-- `snoozed -> expired`) are added defensively for symmetry; the existing
-- triggers reject them today but nothing in the apply path attempts
-- those moves. Including them keeps the table consistent with the
-- `*-> expired` family already present for `new`, `queued`, `ranked`.

BEGIN;

INSERT INTO opp_state_transitions_allowed (from_state, to_state) VALUES
    ('applied',  'expired'),
    ('digested', 'expired'),
    ('seen',     'expired'),
    ('snoozed',  'expired')
ON CONFLICT DO NOTHING;

INSERT INTO schema_migrations (version) VALUES ('V024') ON CONFLICT DO NOTHING;

COMMIT;
