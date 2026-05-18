"""ProxyResolver — no-op in Phase 1 (home ISP). Residential pool lands in Phase 4."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ProxyConfig:
    url: str | None = None
    sticky_session_id: str | None = None


class ProxyResolver:
    async def resolve(self, source_slug: str, identity_id: int | None = None) -> ProxyConfig:
        return ProxyConfig(url=None)
