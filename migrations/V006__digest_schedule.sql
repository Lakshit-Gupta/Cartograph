-- V006__digest_schedule.sql
-- Per-user daily digest schedule. Storage = UTC; conversion from user-local
-- HHMM happens in the /digest schedule slash command using users.timezone.

BEGIN;

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS digest_hour_utc   SMALLINT NOT NULL DEFAULT 2,
    ADD COLUMN IF NOT EXISTS digest_minute_utc SMALLINT NOT NULL DEFAULT 30,
    ADD COLUMN IF NOT EXISTS digest_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

INSERT INTO schema_migrations (version) VALUES ('V006') ON CONFLICT DO NOTHING;

COMMIT;
