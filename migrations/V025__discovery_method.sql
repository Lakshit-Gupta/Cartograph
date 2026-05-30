-- V025__discovery_method.sql
-- Per-source discovery-method enum — decouples the Pi-side URL crawler from
-- the ThinkPad camoufox discovery worker.
--
-- Context (2026-05-29): Internshala's category/location facets are JS-driven
-- dropdowns the curl_cffi crawler can't enumerate, so discovery moves to a
-- camoufox-driven worker on the spare desktop (see
-- docs/superpowers/specs/2026-05-29-internshala-browser-discovery-design.md).
-- This column is the switch the Pi scheduler reads: sources whose
-- discovery_method != 'http_curl' are never emitted as stream:fetch tasks —
-- the discovery worker handles them out-of-band.
--
-- Flag-flippable rollback: `UPDATE sources SET discovery_method = 'http_curl'
-- WHERE slug = 'in_internshala';` resumes the Pi URL crawler within one
-- scheduler tick (the old crawler stays in the tree, dormant, until 7 clean
-- days justify deleting it).

BEGIN;

ALTER TABLE sources
  ADD COLUMN discovery_method TEXT NOT NULL DEFAULT 'http_curl'
    CHECK (discovery_method IN ('http_curl', 'camoufox_dropdown'));

UPDATE sources SET discovery_method = 'camoufox_dropdown' WHERE slug = 'in_internshala';

CREATE INDEX idx_sources_discovery_method ON sources (discovery_method);

INSERT INTO schema_migrations (version) VALUES ('V025') ON CONFLICT DO NOTHING;

COMMIT;
