BEGIN;

-- ---------------------------------------------------------------
-- V013: Phase 3.3 — bounty lane sources.
--
-- Three public bounty platforms scraped into freelance Opportunity rows:
--   - Algora: public JSON feed (no auth, no JS).
--   - Replit Bounties: listing page HTML (no auth, simple HTML).
--   - Gitcoin: public REST API filtered to open bounties.
--
-- Each platform's listing dedupes via canonical apply_url. Stale bounty
-- filter (>14 days old) lives in the extractor, not the source row.
-- ---------------------------------------------------------------

INSERT INTO sources (slug, name, category, base_url, crawler_strategy,
                     fetch_freq_minutes, priority, status, robots_respected,
                     ranking_weight, notes)
VALUES
  ('bounty_algora', 'Algora Bounties', 'freelance',
   'https://console.algora.io/api/v1/bounties/feed.json',
   'bounty_algora', 60, 6, 'active', TRUE, 1.0,
   'Phase 3.3 — Algora public JSON feed. No auth, no JS.'),
  ('bounty_replit', 'Replit Bounties', 'freelance',
   'https://replit.com/bounties',
   'bounty_replit', 60, 6, 'active', TRUE, 1.0,
   'Phase 3.3 — Replit Bounties listing page (HTML scrape via selectolax).'),
  ('bounty_gitcoin', 'Gitcoin', 'freelance',
   'https://gitcoin.co/api/v1/bounty/?status=open&order_by=-web3_created',
   'bounty_gitcoin', 60, 6, 'active', TRUE, 1.0,
   'Phase 3.3 — Gitcoin REST API (open bounties; sorted newest first).')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO schema_migrations (version) VALUES ('V013') ON CONFLICT DO NOTHING;

COMMIT;
