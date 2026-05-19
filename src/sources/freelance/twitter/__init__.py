"""Twitter/X founder-signal fetcher package (Phase 3.1).

Internal subpackage split out of `twitter_fetcher.py` (was 553 LOC). The
public, test-facing module remains `src.sources.freelance.twitter_fetcher`,
which re-exports / hosts the symbols tests touch (so `monkeypatch.setattr`
on `twitter_fetcher` keeps working).

Modules:
    parser   — TweetMatch, parse_tweet_html, tweet_to_opportunity, regex
    mirrors  — _MirrorRotator + Nitter mirror constants
    cap      — _DailyBudget per-handle UTC-day cap
    poller   — fetch_handle + _poll_once + prefs/source-id helpers
"""

from __future__ import annotations
