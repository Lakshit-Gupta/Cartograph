-- V004__opp_state_machine.sql
-- Trigger that auto-logs state transitions to opportunity_transitions
-- and enforces the legal transition graph.

BEGIN;

-- Legal transitions: tuples (from, to) — anything else rejected unless trigger param 'force'=true
-- Implemented as a small lookup table for easy edits.
CREATE TABLE IF NOT EXISTS opp_state_transitions_allowed (
    id         BIGSERIAL PRIMARY KEY,
    from_state opp_state_enum,
    to_state   opp_state_enum NOT NULL
);
-- Logical uniqueness on (from_state, to_state) where from_state can be NULL.
-- Cannot use `COALESCE(from_state::text, '')` — enum->text cast is STABLE
-- (Postgres treats it as STABLE because ALTER TYPE can rename labels), and
-- UNIQUE INDEX expressions must be IMMUTABLE. Two partial indexes give the
-- same logical uniqueness without any function calls:
--   (a) one row per (from_state, to_state) for transitions with a from_state
--   (b) at most one row per to_state for the "initial" NULL -> X transitions
CREATE UNIQUE INDEX IF NOT EXISTS uniq_opp_state_transitions_allowed_nonnull
    ON opp_state_transitions_allowed (from_state, to_state)
    WHERE from_state IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uniq_opp_state_transitions_allowed_initial
    ON opp_state_transitions_allowed (to_state)
    WHERE from_state IS NULL;

INSERT INTO opp_state_transitions_allowed (from_state, to_state) VALUES
    (NULL,        'new'),
    ('new',       'queued'),
    ('queued',    'ranked'),
    ('ranked',    'digested'),
    ('digested',  'seen'),
    ('digested',  'snoozed'),
    ('digested',  'applied'),
    ('seen',      'applied'),
    ('seen',      'snoozed'),
    ('snoozed',   'digested'),
    ('snoozed',   'applied'),
    ('applied',   'interview'),
    ('applied',   'rejected'),
    ('applied',   'withdrawn'),
    ('interview', 'offer'),
    ('interview', 'rejected'),
    ('interview', 'withdrawn'),
    ('offer',     'rejected'),
    ('offer',     'withdrawn'),
    ('new',       'expired'),
    ('queued',    'expired'),
    ('ranked',    'expired'),
    ('digested',  'expired'),
    ('seen',      'expired'),
    ('snoozed',   'expired')
ON CONFLICT DO NOTHING;

CREATE OR REPLACE FUNCTION enforce_opp_state_transition()
RETURNS TRIGGER AS $$
DECLARE
    legal BOOLEAN;
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.state IS DISTINCT FROM NEW.state THEN
        SELECT EXISTS (
            SELECT 1 FROM opp_state_transitions_allowed
            WHERE from_state IS NOT DISTINCT FROM OLD.state
              AND to_state = NEW.state
        ) INTO legal;

        IF NOT legal THEN
            RAISE EXCEPTION 'Illegal opp state transition: % -> %', OLD.state, NEW.state
                USING ERRCODE = 'check_violation';
        END IF;

        INSERT INTO opportunity_transitions(opportunity_id, from_state, to_state, trigger)
        VALUES (NEW.id, OLD.state, NEW.state, 'auto');
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_enforce_opp_state ON opportunities;
CREATE TRIGGER trg_enforce_opp_state
    BEFORE UPDATE OF state ON opportunities
    FOR EACH ROW EXECUTE FUNCTION enforce_opp_state_transition();

-- Identity ban cascade — quarantine siblings sharing fingerprint_id
CREATE OR REPLACE FUNCTION cascade_identity_ban()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.ban_status = 'banned' AND (OLD.ban_status IS DISTINCT FROM 'banned') THEN
        UPDATE identities
        SET ban_status = 'quarantined'
        WHERE fingerprint_id = NEW.fingerprint_id
          AND id <> NEW.id
          AND ban_status = 'healthy';

        INSERT INTO identity_audit(identity_id, action, actor, metadata)
        VALUES (NEW.id, 'ban_cascade_triggered', 'system',
                jsonb_build_object('fingerprint_id', NEW.fingerprint_id));
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_identity_ban_cascade ON identities;
CREATE TRIGGER trg_identity_ban_cascade
    AFTER UPDATE OF ban_status ON identities
    FOR EACH ROW EXECUTE FUNCTION cascade_identity_ban();

INSERT INTO schema_migrations (version) VALUES ('V004') ON CONFLICT DO NOTHING;

COMMIT;
