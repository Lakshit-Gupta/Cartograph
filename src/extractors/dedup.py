"""Dedup helpers — canonical URL + fingerprint hash."""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from src.common.db import acquire
from src.common.metrics import dedup_hits_total

_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
    "gclid",
    "gclsrc",
    "fbclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
    "referer",
    "referrer",
    "icid",
    "cid",
    "lt",
    "src",
    "source",
    "sourceid",
}


def canonicalize_url(url: str) -> str:
    p = urlparse(url)
    qs = parse_qs(p.query, keep_blank_values=False)
    cleaned = {k: v for k, v in qs.items() if k.lower() not in _TRACKING_PARAMS}
    return urlunparse(
        p._replace(
            query=urlencode(cleaned, doseq=True),
            fragment="",
        )
    )


def fp_components(*, company: str | None, title: str, location: str | None, posted_iso: str | None, lane: str) -> str:
    bucket = (posted_iso or "")[:10]
    raw = f"{(company or '').strip().lower()}|{title.strip().lower()}|{(location or '').strip().lower()}|{bucket}|{lane}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()  # noqa: S324


async def already_known(canonical_url: str, fingerprint: str) -> bool:
    async with acquire() as conn:
        rec = await conn.fetchrow(
            """
            SELECT id FROM opportunities
            WHERE canonical_url = $1 OR fingerprint_hash = $2
            LIMIT 1
            """,
            canonical_url,
            fingerprint,
        )
    if rec is not None:
        dedup_hits_total.labels(lane="all").inc()
        return True
    return False
