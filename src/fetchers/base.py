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
    # Optional identity context spliced in by the crawler when the source has
    # `sources.auth_account_id` set. See `src/workers/crawler.py` for the
    # lease/release lifecycle and `src/common/identity_vault.py` for the
    # encryption boundary. Both fields are None for anonymous fetches —
    # which is the steady state until sock-puppet accounts are seeded.
    cookies: dict[str, str] | None = None
    ua_string: str | None = None


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
