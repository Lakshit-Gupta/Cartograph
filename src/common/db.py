"""asyncpg pool + tiny query helpers + tenant resolver hook."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator, Generator
from contextvars import ContextVar
from typing import Any

import asyncpg
from pgvector.asyncpg import register_vector

from src.common.logger import get_logger
from src.common.secrets import get_settings

_log = get_logger(__name__)

_pool: asyncpg.Pool | None = None

# Phase 4.2 multi-tenant resolver.
#
# Every per-tenant code path reads this var instead of hardcoding `1`.
# Default = 1 (the founding solo owner row inserted by V001) so single-tenant
# code paths and offline tests keep working without scaffolding. Workers,
# Discord interaction handlers, and CLI commands set the var explicitly via
# `set_tenant()` / `with_tenant()` before issuing per-user queries.
#
# Why ContextVar rather than a thread-local? asyncpg + discord.py run on
# asyncio; ContextVar is task-scoped and copied into every awaited child,
# so a tenant set in a Discord handler propagates into every DB call that
# handler issues even across `await` points, without leaking into the
# scheduler task running concurrently for a different tenant.
_current_tenant: ContextVar[int] = ContextVar("current_tenant", default=1)


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Per-connection setup, called by asyncpg on every new pool connection.

    Registers the pgvector codec so we can bind Python lists / numpy arrays
    directly into `vector(N)` columns. Without this, asyncpg raises
    `DataError: invalid input for query argument $1 (expected str, got list)`
    even when the SQL has a `$1::vector` cast — the cast happens server-side,
    but the Python -> wire encode still needs the codec.
    """
    await register_vector(conn)


async def init_pool(min_size: int = 2, max_size: int = 10) -> asyncpg.Pool:
    global _pool
    if _pool is None:
        settings = get_settings()
        _pool = await asyncpg.create_pool(
            settings.postgres_dsn,
            min_size=min_size,
            max_size=max_size,
            command_timeout=30,
            server_settings={"application_name": "cartograph"},
            init=_init_connection,
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


def current_tenant() -> int:
    """Return the active tenant id for the current asyncio task.

    Reads `_current_tenant`. Default = 1 (V001 founding owner) when no
    handler has set the var — keeps single-tenant call sites + tests trivial.
    Callers that need a *resolved* tenant (i.e. would error rather than
    silently fall back to the founder) should grab the var directly.
    """
    return _current_tenant.get()


def set_tenant(user_id: int) -> None:
    """Pin the tenant on the current asyncio task. Used by long-running
    workers that process exactly one tenant per loop iteration (e.g. the
    Discord interaction handler after `_resolve_tenant_from_discord_user`).
    """
    _current_tenant.set(user_id)


@contextlib.contextmanager
def with_tenant(user_id: int) -> Generator[None, None, None]:
    """Scoped tenant override. Resets to the prior value on exit, even on
    exception — safe to use around DB calls inside a handler that runs
    concurrently with other tenants on the same event loop.
    """
    token = _current_tenant.set(user_id)
    try:
        yield
    finally:
        _current_tenant.reset(token)


# Backwards-compatible alias — keep the Phase 1 name working while callers
# migrate. New code should call `current_tenant()` directly.
def current_user_id() -> int:
    return current_tenant()
