-- V026__discovery_cycle_log.sql
-- Per-cycle observability table for the camoufox discovery worker.
--
-- Context (2026-05-29): the ThinkPad discovery worker writes one row per
-- discovery cycle (the DiscoveryCycleReport) so we can reconstruct "why did
-- this cycle scrape N cards / publish M / drop K" from a single query, and
-- so the Discord #🛠-source-health card has a durable backing record. The
-- combo_* / selector_misses arrays capture per-cycle dropdown-combo failures
-- and selector drift, which is the primary signal for "Internshala changed
-- their DOM again" without needing to tail sidecar logs.
--
-- See docs/superpowers/specs/2026-05-29-internshala-browser-discovery-design.md.

BEGIN;

CREATE TABLE discovery_cycle_log (
  id                       BIGSERIAL PRIMARY KEY,
  cycle_id                 UUID        NOT NULL UNIQUE,
  worker_id                TEXT        NOT NULL,
  source_slug              TEXT        NOT NULL,
  started_at               TIMESTAMPTZ NOT NULL,
  duration_sec             REAL        NOT NULL,
  combos_attempted         INT         NOT NULL,
  combos_succeeded         INT         NOT NULL,
  combo_timeouts           TEXT[]      NOT NULL DEFAULT '{}',
  selector_misses          TEXT[]      NOT NULL DEFAULT '{}',
  cards_scraped            INT         NOT NULL,
  cards_published          INT         NOT NULL,
  cards_rejected_subfloor  INT         NOT NULL,
  cards_rejected_dedup     INT         NOT NULL,
  cards_rejected_parse     INT         NOT NULL,
  healthy                  BOOLEAN     NOT NULL,
  selectors_version        TEXT        NOT NULL,
  matrix_version           TEXT        NOT NULL,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_discovery_cycle_log_started_at ON discovery_cycle_log (started_at DESC);
CREATE INDEX idx_discovery_cycle_log_source_slug ON discovery_cycle_log (source_slug, started_at DESC);

INSERT INTO schema_migrations (version) VALUES ('V026') ON CONFLICT DO NOTHING;

COMMIT;
