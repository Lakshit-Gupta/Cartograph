-- V020__seed_notification_routes.sql
-- Phase 3 follow-on — persist Discord channel routes in `notification_routes`
-- so per-tenant onboarding can override channel IDs without an env / SOPS edit
-- + container restart.
--
-- Schema delta:
--   * V001 PK was (user_id, channel, route_type). The route_type enum only has
--     5 values but Cartograph has 14 logical Discord channels (one row per
--     logical name: daily_digest, fulltime, internships, ...). Promoting
--     `target` (the logical channel name) into the PK lets us store every
--     logical channel as its own row while keeping the route_type bucket as
--     metadata that the handlers already use (lane vs tracker vs alerts).
--   * No data migration needed — the table is empty in every existing
--     deployment (Phase 1–2 read channel IDs from settings env, never wrote
--     here). Dropping & recreating the PK is safe.
--
-- Seed strategy:
--   Each row carries the logical channel name as `target` plus the metadata a
--   handler needs (route_type for grouping, embed_color for lane rows). The
--   actual numeric Discord channel ID is intentionally left NULL here — V001
--   default rows must be valid against any future seed migration replay, and
--   per-deployment IDs live in SOPS-encrypted env. The runtime loader
--   (`src/notifiers/discord/routing_db.py`) reads this row, sees
--   discord_channel_id IS NULL, and falls back to settings.discord_channel(name)
--   on first read. The CLI (`mp routes set <name> <id>`) writes the real ID
--   when the operator promotes a value out of env into the DB (multi-tenant
--   onboarding flow).
--
-- Rollback (manual, never via `down --volumes`):
--   DELETE FROM notification_routes WHERE user_id = 1;
--   ALTER TABLE notification_routes DROP CONSTRAINT notification_routes_pkey;
--   ALTER TABLE notification_routes ADD PRIMARY KEY (user_id, channel, route_type);

BEGIN;

-- =========================================================================
-- 1. Widen PK so multiple logical channels coexist per (user, channel).
-- =========================================================================
ALTER TABLE notification_routes DROP CONSTRAINT IF EXISTS notification_routes_pkey;
ALTER TABLE notification_routes ADD PRIMARY KEY (user_id, channel, target);

-- Defensive index so the route_type bucket queries (e.g. "all tracker rows
-- for user X") stay O(log n) even when 14+ rows per user accumulate.
CREATE INDEX IF NOT EXISTS idx_notification_routes_route_type
    ON notification_routes (user_id, channel, route_type);

-- =========================================================================
-- 2. Seed the 14 logical Discord channels for the founding owner (user_id=1).
--
-- ON CONFLICT DO NOTHING because:
--   * V020 must be idempotent — replaying migrations on a fresh
--     pgvector container (validate_migrations.sh) and on prod must both work.
--   * Operators may edit rows post-seed via `mp routes set`; re-running
--     `mp migrate` must NOT clobber their custom discord_channel_id.
-- =========================================================================
INSERT INTO notification_routes (user_id, channel, route_type, target, enabled, embed_color)
VALUES
    -- Digest + push lanes (route_type maps to V001 enum).
    (1, 'discord', 'daily_digest',  'daily_digest',  TRUE, x'6B7280'::int),  -- gray (defaults)
    (1, 'discord', 'priority_push', 'priority_push', TRUE, x'EF4444'::int),  -- red (urgent)

    -- Per-lane forum channels. route_type=daily_digest groups them in the
    -- bucket index above; handlers route via target (logical name).
    (1, 'discord', 'daily_digest', 'fulltime',    TRUE, x'4F46E5'::int),     -- indigo
    (1, 'discord', 'daily_digest', 'internships', TRUE, x'10B981'::int),     -- emerald
    (1, 'discord', 'daily_digest', 'fellowships', TRUE, x'F59E0B'::int),     -- amber
    (1, 'discord', 'daily_digest', 'freelance',   TRUE, x'EC4899'::int),     -- pink

    -- Tracker forum + text channels.
    (1, 'discord', 'tracker', 'applied',    TRUE, NULL),
    (1, 'discord', 'tracker', 'responses',  TRUE, NULL),
    (1, 'discord', 'tracker', 'interviews', TRUE, NULL),
    (1, 'discord', 'tracker', 'offers',     TRUE, NULL),

    -- System channels.
    (1, 'discord', 'alerts', 'alerts',        TRUE, NULL),
    (1, 'discord', 'costs',  'costs',         TRUE, NULL),
    (1, 'discord', 'alerts', 'source_health', TRUE, NULL),
    (1, 'discord', 'alerts', 'bot_logs',      TRUE, NULL)
ON CONFLICT (user_id, channel, target) DO NOTHING;

INSERT INTO schema_migrations (version) VALUES ('V020') ON CONFLICT DO NOTHING;

COMMIT;
