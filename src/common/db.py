"""asyncpg pool + tiny query helpers + tenant resolver hook."""
from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator
from typing import Any

import asyncpg

from src.common.logger import get_logger
from src.common.secrets import get_settings

_log = get_logger(__name__)

_pool: asyncpg.Pool | None = None


async def init_pool(min_size: int = 2, max_size: int = 10) -> asyncpg.Pool:
    global _pool
    if _pool is None:
        settings = get_settings()
        _pool = await asyncpg.create_pool(
            settings.postgres_dsn,
            min_size=min_size,
            max_size=max_size,
            command_timeout=30,
            server_settings={"application_name": "marked_path"},
        )
        _log.info("postgres_pool_ready", min_size=min_size, max_size=max_size)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised. Call init_pool() first.")
    return _pool


@contextlib.asynccontextmanager
async def acquire() -> AsyncGenerator[asyncpg.Connection, None]:
    pool = get_pool()
    async with pool.acquire() as conn:
        yield conn


async def fetch_one(query: str, *args: Any) -> asyncpg.Record | None:
    async with acquire() as conn:
        return await conn.fetchrow(query, *args)


async def fetch_all(query: str, *args: Any) -> list[asyncpg.Record]:
    async with acquire() as conn:
        return await conn.fetch(query, *args)


async def execute(query: str, *args: Any) -> str:
    async with acquire() as conn:
        return await conn.execute(query, *args)


async def execute_many(query: str, args_list: list[tuple[Any, ...]]) -> None:
    async with acquire() as conn:
        await conn.executemany(query, args_list)


# Phase 4 multi-tenant: resolved from middleware or worker leasing context.
# Phase 1: hardcoded solo owner.
def current_user_id() -> int:
    return 1
