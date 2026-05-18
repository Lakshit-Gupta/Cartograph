"""Prometheus /metrics endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Response

from src.common.metrics import render_metrics

router = APIRouter()


@router.get("/metrics")
async def metrics() -> Response:
    body, content_type = render_metrics()
    return Response(content=body, media_type=content_type)
