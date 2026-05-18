"""Extractor Protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from src.common.types import Opportunity


@dataclass(slots=True)
class ExtractInput:
    source_id: int
    source_slug: str
    url: str
    content: str
    content_type: str | None


@dataclass(slots=True)
class ExtractOutput:
    opps: list[Opportunity]
    tier_used: int
    confidence: float


class Extractor(Protocol):
    tier: int

    async def extract(self, inp: ExtractInput) -> ExtractOutput: ...
