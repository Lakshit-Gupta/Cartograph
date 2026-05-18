"""Source crawler Protocol — every source plugin returns a list of URLs to fetch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(slots=True)
class CrawlPlan:
    """A list of fetch targets a source wants the crawler workers to chew through."""

    source_id: int
    source_slug: str
    urls: list[str]
    tier_chain: list[int]
    requires_identity: bool = False


class SourcePlugin(Protocol):
    slug: str
    strategy: str

    async def plan(self, *, source_id: int, base_url: str, config: dict) -> CrawlPlan: ...
