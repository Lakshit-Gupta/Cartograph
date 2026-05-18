-- V005__cf_clearance_cache_indexes.sql
-- Hot-path indexes for CF clearance cache reuse + housekeeping.

BEGIN;

CREATE INDEX IF NOT EXISTS idx_cf_cache_domain_expires
    ON cf_clearance_cache(domain, expires_at);

-- Note: cannot use `WHERE expires_at > NOW()` — NOW() is STABLE, not IMMUTABLE,
-- and partial-index predicates must be IMMUTABLE. Plain index on (source_id,
-- expires_at) lets the planner do an index range scan for `WHERE source_id=X
-- AND expires_at > now()` queries — same hot path, no IMMUTABLE violation.
CREATE INDEX IF NOT EXISTS idx_cf_cache_source_expires
    ON cf_clearance_cache(source_id, expires_at);

CREATE INDEX IF NOT EXISTS idx_cf_cache_last_used
    ON cf_clearance_cache(last_used_at DESC NULLS LAST);

-- Convenience view: live clearances per source/domain
CREATE OR REPLACE VIEW cf_clearance_live AS
SELECT *
FROM cf_clearance_cache
WHERE expires_at > NOW();

INSERT INTO schema_migrations (version) VALUES ('V005') ON CONFLICT DO NOTHING;

COMMIT;
