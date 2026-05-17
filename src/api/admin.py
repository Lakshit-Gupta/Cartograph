"""Admin endpoints — exposed only via Tailscale-bound ingress.

Public ingress through cloudflared whitelists only `/webhooks/*`; the rest of
the FastAPI surface lives behind Tailscale ACLs (configured at the host).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from src.common.db import acquire

router = APIRouter(prefix="/admin")


@router.get("/sources")
async def list_sources() -> list[dict]:
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, slug, name, category, status, fetch_freq_minutes, priority,
                   cf_protection_level, last_successful_crawl_at, opps_extracted_30d
            FROM sources ORDER BY priority DESC, slug
            """
        )
    return [dict(r) for r in rows]


@router.post("/sources/{slug}/pause")
async def pause_source(slug: str) -> dict:
    async with acquire() as conn:
        res = await conn.execute(
            "UPDATE sources SET status = 'paused' WHERE slug = $1",
            slug,
        )
    if res.endswith("0"):
        raise HTTPException(status_code=404, detail="source not found")
    return {"status": "paused", "slug": slug}


@router.post("/sources/{slug}/resume")
async def resume_source(slug: str) -> dict:
    async with acquire() as conn:
        res = await conn.execute(
            "UPDATE sources SET status = 'active' WHERE slug = $1",
            slug,
        )
    if res.endswith("0"):
        raise HTTPException(status_code=404, detail="source not found")
    return {"status": "active", "slug": slug}


@router.get("/identities")
async def list_identities() -> list[dict]:
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, platform, account_label, ban_status, warmup_score, warmup_completed,
                   last_used_at
            FROM identities ORDER BY platform, account_label
            """
        )
    return [dict(r) for r in rows]


@router.get("/cost/today")
async def cost_today() -> dict:
    async with acquire() as conn:
        rec = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(cost_usd_micros),0) AS m
            FROM usage_ledger WHERE ts::date = CURRENT_DATE
            """
        )
    return {"usd": float(rec["m"]) / 1_000_000.0 if rec else 0.0}
