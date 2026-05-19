"""Phase 3.4 — OSS contribution funnel.

Sweeps `target_companies WHERE active=true AND github_org IS NOT NULL`
on a daily 08:00 IST cron, queries the GitHub Search API for the
canonical `good first issue` label on each company's org, and emits
filtered issues onto `stream:rank` as Opportunity rows.

Unlike the crawler-tier source plugins (`src.sources.ats/*`,
`src.sources.github_markdown/*`), this lane does NOT register a
`SourcePlugin` in the strategy registry. The crawler dispatcher's
FetchTask/FetchResult cycle is designed for HTML/JSON pages that need
extractor cascades; the GitHub Search API returns ready-to-rank issue
objects with strict schemas — running it through extractors would add
LLM cost for zero signal. So we publish straight to `stream:rank` via
`extractors.persist.persist_and_publish`, the same write path the
freelance Telegram lane uses.
"""

from __future__ import annotations

from src.sources.oss_funnel.github_issues import (
    GitHubIssueScanner,
    fingerprint_for,
    parse_issue_to_opportunity,
)

__all__ = [
    "GitHubIssueScanner",
    "fingerprint_for",
    "parse_issue_to_opportunity",
]
