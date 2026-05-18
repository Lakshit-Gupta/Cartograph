-- V003__sources_seed.sql
-- Seeds 28+ initial sources. Real slugs come from config/sources/*.yaml via
-- `python -m src.cli.main seed-sources`. This migration creates the meta-row
-- per source family; the YAML loader fills per-slug crawl targets at runtime.

BEGIN;

-- Helper to upsert a source meta row
CREATE OR REPLACE FUNCTION _seed_source(
    p_slug TEXT, p_name TEXT, p_category TEXT, p_base_url TEXT,
    p_strategy TEXT, p_freq INTEGER, p_priority INTEGER, p_cf TEXT,
    p_tier_chain INTEGER[], p_browser BOOLEAN
) RETURNS VOID AS $$
BEGIN
    INSERT INTO sources(
        slug, name, category, base_url, crawler_strategy,
        fetch_freq_minutes, priority, cf_protection_level,
        tier_chain, browser_mode_required, status, created_via
    ) VALUES (
        p_slug, p_name, p_category, p_base_url, p_strategy,
        p_freq, p_priority, p_cf, p_tier_chain, p_browser, 'active', 'seed'
    )
    ON CONFLICT (slug) DO UPDATE
        SET name = EXCLUDED.name,
            category = EXCLUDED.category,
            base_url = EXCLUDED.base_url,
            crawler_strategy = EXCLUDED.crawler_strategy,
            fetch_freq_minutes = EXCLUDED.fetch_freq_minutes,
            priority = EXCLUDED.priority,
            cf_protection_level = EXCLUDED.cf_protection_level,
            tier_chain = EXCLUDED.tier_chain,
            browser_mode_required = EXCLUDED.browser_mode_required,
            updated_at = NOW();
END;
$$ LANGUAGE plpgsql;

-- ATS — JSON APIs, no CF
SELECT _seed_source('ats_greenhouse',  'Greenhouse boards',  'ats',         'https://boards-api.greenhouse.io', 'ats_greenhouse',  30, 9, 'none',  ARRAY[0], FALSE);
SELECT _seed_source('ats_lever',       'Lever postings',     'ats',         'https://api.lever.co',             'ats_lever',       30, 9, 'none',  ARRAY[0], FALSE);
SELECT _seed_source('ats_ashby',       'Ashby job boards',   'ats',         'https://api.ashbyhq.com',          'ats_ashby',       30, 9, 'none',  ARRAY[0], FALSE);
SELECT _seed_source('ats_workable',    'Workable companies', 'ats',         'https://apply.workable.com',       'ats_workable',    60, 8, 'none',  ARRAY[0], FALSE);

-- RSS
SELECT _seed_source('rss_remoteok',         'RemoteOK feed',          'rss',  'https://remoteok.com/remote-jobs.rss',          'rss_generic', 60, 7, 'none', ARRAY[0], FALSE);
SELECT _seed_source('rss_weworkremotely',   'WeWorkRemotely feed',    'rss',  'https://weworkremotely.com/remote-jobs.rss',    'rss_generic', 60, 7, 'none', ARRAY[0], FALSE);

-- GitHub markdown lists
SELECT _seed_source('gh_simplifyjobs', 'SimplifyJobs Summer 2026',  'github_md', 'https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/README.md', 'github_md', 60, 8, 'none', ARRAY[0], FALSE);
SELECT _seed_source('gh_ouckah',       'Ouckah/Summer2025',         'github_md', 'https://raw.githubusercontent.com/Ouckah/Summer2025-Internships/main/README.md',     'github_md', 60, 7, 'none', ARRAY[0], FALSE);
SELECT _seed_source('gh_pittcsc',      'SimplifyJobs New-Grad',     'github_md', 'https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md',    'github_md', 60, 7, 'none', ARRAY[0], FALSE);

-- HN / Reddit
SELECT _seed_source('hn_algolia',      'HN Who is hiring',  'hn',     'https://hn.algolia.com/api/v1/search_by_date', 'hn_algolia',    180, 7, 'none', ARRAY[0], FALSE);
SELECT _seed_source('reddit_forhire',  'r/forhire',         'reddit', 'https://www.reddit.com/r/forhire/new.json',    'reddit_oauth',   15, 8, 'none', ARRAY[0], FALSE);
SELECT _seed_source('reddit_remotejs', 'r/remotejs',        'reddit', 'https://www.reddit.com/r/remotejs/new.json',   'reddit_oauth',   60, 6, 'none', ARRAY[0], FALSE);

-- Fellowships (mix of HTML scraping + RSS)
SELECT _seed_source('fellow_anthropic',  'Anthropic Fellows',   'fellowship', 'https://www.anthropic.com/jobs', 'fellowship_html', 1440, 9, 'managed', ARRAY[0,1,2], FALSE);
SELECT _seed_source('fellow_mats',       'MATS Program',        'fellowship', 'https://www.matsprogram.org/',   'fellowship_html', 1440, 8, 'none',    ARRAY[0],     FALSE);
SELECT _seed_source('fellow_openai_res', 'OpenAI Residency',    'fellowship', 'https://openai.com/careers',     'fellowship_html', 1440, 9, 'managed', ARRAY[0,1,2], FALSE);
SELECT _seed_source('fellow_ml_collect', 'ML Collective',       'fellowship', 'https://mlcollective.org/',      'fellowship_html', 1440, 7, 'none',    ARRAY[0],     FALSE);
SELECT _seed_source('fellow_cohere',     'Cohere For AI',       'fellowship', 'https://cohere.com/research/scholars-program', 'fellowship_html', 1440, 7, 'none', ARRAY[0], FALSE);
SELECT _seed_source('fellow_hf',         'HuggingFace Fellows', 'fellowship', 'https://huggingface.co/blog?tag=fellows', 'fellowship_html', 1440, 7, 'none', ARRAY[0], FALSE);
SELECT _seed_source('fellow_yc',         'YC Fellows',          'fellowship', 'https://www.ycombinator.com/companies', 'fellowship_html', 1440, 7, 'none', ARRAY[0], FALSE);

-- India
SELECT _seed_source('in_internshala',  'Internshala',       'india',  'https://internshala.com/internships', 'india_internshala',  60, 8, 'basic',   ARRAY[0,1], FALSE);
SELECT _seed_source('in_cuvette',      'Cuvette',           'india',  'https://api.cuvette.tech',            'india_cuvette',      60, 8, 'basic',   ARRAY[0],   FALSE);
SELECT _seed_source('in_unstop',       'Unstop',            'india',  'https://unstop.com/api',              'india_unstop',       60, 7, 'basic',   ARRAY[0],   FALSE);
SELECT _seed_source('in_yc_india',     'YC India',          'india',  'https://www.ycombinator.com/companies?country=India', 'india_yc',  1440, 7, 'none', ARRAY[0], FALSE);
SELECT _seed_source('in_inc42',        'Inc42 Funding',     'india',  'https://inc42.com/funding/',          'india_inc42',       720, 6, 'basic',   ARRAY[0,1,2], FALSE);
SELECT _seed_source('in_yourstory',    'YourStory Funding', 'india',  'https://yourstory.com/funding',       'india_yourstory',   720, 6, 'basic',   ARRAY[0,1,2], FALSE);

-- Freelance speed lane
SELECT _seed_source('fl_contra',          'Contra hot opps',    'freelance', 'https://contra.com/opportunities',          'freelance_contra',     2,  10, 'managed', ARRAY[0,1,2], FALSE);
SELECT _seed_source('fl_upwork_email',    'Upwork email digest','freelance', 'imap:gmail-worker',                          'freelance_upwork_im',  5,  9,  'none',    ARRAY[0],     FALSE);
SELECT _seed_source('fl_telegram',        'Telegram channels',  'freelance', 'tg://channels',                              'freelance_telegram',   5,  9,  'none',    ARRAY[0],     FALSE);
SELECT _seed_source('fl_forhire_push',    'r/forhire push',     'freelance', 'https://www.reddit.com/r/forhire/new.json',  'reddit_oauth_push',    2,  10, 'none',    ARRAY[0],     FALSE);

DROP FUNCTION IF EXISTS _seed_source(TEXT, TEXT, TEXT, TEXT, TEXT, INTEGER, INTEGER, TEXT, INTEGER[], BOOLEAN);

INSERT INTO schema_migrations (version) VALUES ('V003') ON CONFLICT DO NOTHING;

COMMIT;
