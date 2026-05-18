BEGIN;

-- ---------------------------------------------------------------
-- V008: Refresh stale GitHub source URLs.
--
-- The Summer2025-Internships repos archived after the cycle closed.
-- PittCSC/NewGrad-Jobs was renamed to SimplifyJobs/New-Grad-Positions
-- when SimplifyJobs took over maintenance. Both moves landed before
-- 2026-05-19. Without this update the github_md crawler hits 404 for
-- gh_pittcsc and connection-refused for gh_simplifyjobs.
--
-- Probed live 2026-05-19:
--   SimplifyJobs/Summer2026-Internships/dev/README.md → 200
--   SimplifyJobs/New-Grad-Positions/dev/README.md     → 200
--   Ouckah/Summer2025-Internships/main/README.md      → 200 (still live)
-- ---------------------------------------------------------------

UPDATE sources
SET base_url = 'https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/README.md',
    last_successful_crawl_at = NULL
WHERE slug = 'gh_simplifyjobs';

UPDATE sources
SET base_url = 'https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md',
    last_successful_crawl_at = NULL
WHERE slug = 'gh_pittcsc';

INSERT INTO schema_migrations(version) VALUES (8) ON CONFLICT DO NOTHING;

COMMIT;
