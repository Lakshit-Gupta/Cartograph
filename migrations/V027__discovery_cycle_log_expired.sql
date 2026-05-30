-- V027__discovery_cycle_log_expired.sql
-- Add the expired-card reject counter to the discovery cycle log.
--
-- Context (2026-05-31): the camoufox discovery worker now drops cards whose
-- "Apply By" deadline has passed (or that are older than max_age_days with no
-- deadline) BEFORE they reach Postgres — see report.passes_validity. The
-- DiscoveryCycleReport gained a `cards_rejected_expired` tally alongside the
-- existing subfloor / dedup / parse rejects; this column gives it a durable
-- home so #🛠-source-health and the cycle-log query show how many expired
-- listings each cycle filtered out.
--
-- Additive + DEFAULT 0 so the ALTER is safe on the existing rows written
-- since V026.

BEGIN;

ALTER TABLE discovery_cycle_log
  ADD COLUMN cards_rejected_expired INT NOT NULL DEFAULT 0;

INSERT INTO schema_migrations (version) VALUES ('V027') ON CONFLICT DO NOTHING;

COMMIT;
