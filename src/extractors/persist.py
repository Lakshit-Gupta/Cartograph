"""Single write-path for opportunities → opportunity row → Streams.RANK enqueue.

Both the extractor cascade (HTML/JSON pages) AND the gmail watcher Upwork lane
(pre-extracted opps inlined from email digests) call this. Keeping the write
path in one place means dedup, state machine, and Streams.RANK contract all
behave identically for every producer.
"""
from __future__ import annotations

from uuid import UUID

from src.common.db import acquire
from src.common.logger import get_logger
from src.common.queue import RedisQ, Streams
from src.common.types import Opportunity
from src.extractors.dedup import already_known, canonicalize_url

_log = get_logger(__name__)


async def persist_and_publish(q: RedisQ, opp: Opportunity, *, user_id: int = 1) -> UUID | None:
    """Upsert one Opportunity into `opportunities`, publish onto Streams.RANK.

    Returns the opportunity id (new or existing) when something was enqueued
    for ranking, else None (dedup hit).
    """
    canon = canonicalize_url(opp.canonical_url)
    if await already_known(canon, opp.fingerprint_hash):
        return None
    async with acquire() as conn:
        rec = await conn.fetchrow(
            """
            INSERT INTO opportunities(
                source_id, canonical_url, title, company, description,
                comp_min, comp_max, comp_currency, comp_period,
                location, remote_type, category,
                posted_at, expires_at, apply_url, apply_method,
                fingerprint_hash, extraction_tier, extraction_confidence,
                state
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::remote_type_enum,$12,$13,$14,$15,$16::apply_method_enum,$17,$18,$19,'queued')
            ON CONFLICT (canonical_url) DO UPDATE SET last_seen = NOW()
            RETURNING id
            """,
            opp.source_id, canon, opp.title, opp.company, opp.description,
            opp.comp_min, opp.comp_max, opp.comp_currency, opp.comp_period,
            opp.location, opp.remote_type.value, opp.category.value,
            opp.posted_at, opp.expires_at, opp.apply_url,
            opp.apply_method.value if opp.apply_method else None,
            opp.fingerprint_hash, opp.extraction_tier, opp.extraction_confidence,
        )
    if rec is None:
        return None
    opp_id = rec["id"]
    await q.publish(Streams.RANK, {"opportunity_id": str(opp_id), "user_id": user_id})
    return opp_id
