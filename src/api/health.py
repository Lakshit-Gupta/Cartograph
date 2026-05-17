"""Health endpoints."""
from __future__ import annotations

from fastapi import APIRouter

from src.common.db import acquire
from src.common.queue import RedisQ

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@router.get("/ready")
async def ready() -> dict:
    pg_ok = redis_ok = False
    try:
        async with acquire() as conn:
            await conn.fetchval("SELECT 1")
        pg_ok = True
    except Exception:
        pg_ok = False
    try:
        q = await RedisQ.connect()
        await q.raw.ping()
        redis_ok = True
    except Exception:
        redis_ok = False
    return {"postgres": pg_ok, "redis": redis_ok}
