"""Fetcher Protocol — every tier implements this. Swappable behind dispatcher."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(slots=True)
class FetchRequest:
    source_id: int
    source_slug: str
    url: str
    method: str = "GET"
    headers: dict[str, str] | None = None
    body: bytes | None = None
    identity_id: int | None = None
    timeout_s: float = 30.0


@dataclass(slots=True)
class FetchResponse:
    status: int
    body: str
    content_type: str | None
    tier: int
    headers: dict[str, str]
    error: str | None = None
    cf_challenge_observed: bool = False


class Fetcher(Protocol):
    tier: int

    async def fetch(self, req: FetchRequest) -> FetchResponse: ...
