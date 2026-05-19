"""FastAPI app — wires health, metrics, admin routes."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.api.admin import router as admin_router
from src.api.dashboard import mount_dashboard
from src.api.health import router as health_router
from src.api.metrics import router as metrics_router
from src.api.postgrest_proxy import aclose_proxy_client
from src.api.postgrest_proxy import router as postgrest_proxy_router
from src.common.db import close_pool, init_pool
from src.common.logger import configure_logging, get_logger

configure_logging("api")
_log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    await init_pool()
    _log.info("api_started")
    try:
        yield
    finally:
        await aclose_proxy_client()
        await close_pool()


app = FastAPI(title="Cartograph API", version="0.1.0", lifespan=lifespan)
app.include_router(health_router)
app.include_router(metrics_router)
app.include_router(admin_router)
# Phase 5.2 — dashboard PostgREST proxy + (guarded) static mount.
app.include_router(postgrest_proxy_router)
mount_dashboard(app)
