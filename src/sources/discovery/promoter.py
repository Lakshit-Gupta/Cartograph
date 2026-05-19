"""Promote classified candidates into the sources table.

Three tiers, set by classifier_confidence:
  - > AUTO_PROMOTE_THRESHOLD (0.85): insert sources row + candidate_sources
    row (status='auto_promoted') + source_provenance row, all in one txn.
  - 0.5 .. 0.85: write candidate_sources row (status='pending'). User reviews
    via /review.
  - < 0.5: drop. Returned in stats as `discarded`.

Idempotence is enforced at the DB layer:
  - candidate_sources has UNIQUE(url) — second write of the same URL raises.
  - sources has UNIQUE(slug). The slug is derived from the host so
    duplicate-host promotions collapse cleanly.

Failure mode: any single-candidate failure logs and continues. The pipeline
keeps the per-strategy counters so the user can see (e.g.) "github strategy
proposed 30, 12 were dupes, 5 auto-promoted, 13 wait review".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

import asyncpg

from src.common import db
from src.common.logger import get_logger
from src.sources.discovery.base import CandidateSource

_log = get_logger(__name__)

AUTO_PROMOTE_THRESHOLD = 0.85
PENDING_THRESHOLD = 0.5

# Map classifier category → (crawler_strategy default, cf_protection_level).
# The auto-promoted sources will likely need refinement, but the defaults
# get them onto the FETCH stream and into the cycle. Operator can tune via
# `/source` slash command later.
_CATEGORY_TO_STRATEGY = {
    "ats": ("generic_html", "basic"),
    "rss": ("rss_generic", "none"),
    "github_md": ("github_md", "none"),
    "hn": ("hn_algolia", "none"),
    "reddit": ("reddit_oauth", "none"),
    "fellowship": ("fellowship_html", "managed"),
    "india": ("india_internshala", "basic"),
    "freelance": ("freelance_contra", "managed"),
    "other": ("generic_html", "basic"),
}


@dataclass
class PromoteStats:
    proposed: int = 0  # number of classifier-passing candidates we saw
    duplicates: int = 0  # URL already in sources or candidate_sources
    auto_promoted: int = 0  # confidence > 0.85
    pending: int = 0  # 0.5 .. 0.85
    discarded: int = 0  # < 0.5
    errors: int = 0  # DB write failures (shouldn't happen)


def _slug_from_url(url: str) -> str:
    """Build a sources.slug-safe identifier from a URL host."""
    host = (urlparse(url).hostname or "").lower()
    host = host.removeprefix("www.")
    # Collapse non-alnum into _
    base = re.sub(r"[^a-z0-9]+", "_", host).strip("_")
    return f"discovered_{base}"[:80] if base else "discovered_unknown"


async def _is_known_url(url: str) -> bool:
    """Already in sources.base_url OR candidate_sources.url?"""
    rec = await db.fetch_one(
        """
        SELECT 1 FROM sources WHERE base_url = $1
        UNION ALL
        SELECT 1 FROM candidate_sources WHERE url = $1
        LIMIT 1
        """,
        url,
    )
    return rec is not None


async def _auto_promote(conn: asyncpg.Connection, candidate: CandidateSource) -> int | None:
    """Insert sources + candidate_sources + source_provenance in one txn.

    Returns the new sources.id on success, None if the slug collided (race
    with another discovery run or manual /source add).
    """
    category = candidate.classifier_category or "other"
    strategy, cf_level = _CATEGORY_TO_STRATEGY.get(category, ("generic_html", "basic"))
    slug = _slug_from_url(candidate.url)

    # 1. sources insert. ON CONFLICT DO NOTHING so a slug collision drops
    #    us to the candidate-only branch below.
    source_row = await conn.fetchrow(
        """
        INSERT INTO sources (
            slug, name, category, base_url, crawler_strategy,
            fetch_freq_minutes, priority, cf_protection_level,
            tier_chain, browser_mode_required, status, created_via,
            discovery_confidence
        ) VALUES (
            $1, $2, $3, $4, $5,
            240, 5, $6,
            ARRAY[0,1,2], FALSE, 'active', 'discovery',
            $7
        )
        ON CONFLICT (slug) DO NOTHING
        RETURNING id
        """,
        slug,
        (candidate.title or candidate.url)[:200],
        category if category in ("ats", "rss", "github_md", "hn", "reddit", "fellowship", "india", "freelance") else "other",
        candidate.url,
        strategy,
        cf_level,
        candidate.classifier_confidence,
    )
    if source_row is None:
        return None
    source_id = int(source_row["id"])

    # 2. candidate_sources row (auto_promoted state).
    cand_row = await conn.fetchrow(
        """
        INSERT INTO candidate_sources (
            url, title, snippet, discovered_via,
            classifier_confidence, classifier_category, classifier_rationale,
            status, promoted_source_id, reviewed_at
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,'auto_promoted',$8,NOW())
        ON CONFLICT (url) DO UPDATE SET
            status = 'auto_promoted',
            promoted_source_id = EXCLUDED.promoted_source_id,
            reviewed_at = NOW()
        RETURNING id
        """,
        candidate.url,
        candidate.title,
        candidate.snippet,
        candidate.discovered_via,
        candidate.classifier_confidence,
        candidate.classifier_category,
        candidate.classifier_rationale,
        source_id,
    )
    candidate_id = int(cand_row["id"]) if cand_row else None

    # 3. provenance row — many-to-one back to candidate.
    await conn.execute(
        """
        INSERT INTO source_provenance (source_id, candidate_source_id, discovered_via)
        VALUES ($1, $2, $3)
        """,
        source_id,
        candidate_id,
        candidate.discovered_via,
    )
    return source_id


async def _write_pending(candidate: CandidateSource) -> bool:
    """Insert a candidate_sources row in 'pending' state. Returns True on insert."""
    try:
        result = await db.execute(
            """
            INSERT INTO candidate_sources (
                url, title, snippet, discovered_via,
                classifier_confidence, classifier_category, classifier_rationale,
                status
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,'pending')
            ON CONFLICT (url) DO NOTHING
            """,
            candidate.url,
            candidate.title,
            candidate.snippet,
            candidate.discovered_via,
            candidate.classifier_confidence,
            candidate.classifier_category,
            candidate.classifier_rationale,
        )
        # asyncpg returns "INSERT 0 1" for inserted rows, "INSERT 0 0" for conflicts.
        return result.endswith(" 1")
    except Exception as e:
        _log.warning("candidate_insert_failed", url=candidate.url, err=str(e))
        return False


async def promote_candidates(candidates: list[CandidateSource]) -> PromoteStats:
    """Apply the three-tier promotion logic. Caller has already classified."""
    stats = PromoteStats()
    for c in candidates:
        if c.classifier_confidence is None:
            stats.discarded += 1
            continue
        stats.proposed += 1
        if c.classifier_confidence < PENDING_THRESHOLD:
            stats.discarded += 1
            continue

        # Dedupe against existing sources + candidate_sources up front to keep
        # the auto-promote txn from racing against itself across re-runs.
        if await _is_known_url(c.url):
            stats.duplicates += 1
            continue

        if c.classifier_confidence >= AUTO_PROMOTE_THRESHOLD:
            try:
                async with db.acquire() as conn, conn.transaction():
                    new_id = await _auto_promote(conn, c)
                if new_id is None:
                    stats.duplicates += 1
                else:
                    stats.auto_promoted += 1
                    _log.info("source_auto_promoted", url=c.url, source_id=new_id, confidence=c.classifier_confidence)
            except Exception as e:
                stats.errors += 1
                _log.warning("auto_promote_failed", url=c.url, err=str(e))
        else:
            if await _write_pending(c):
                stats.pending += 1
            else:
                stats.duplicates += 1
    return stats
