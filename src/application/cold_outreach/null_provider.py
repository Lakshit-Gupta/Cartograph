"""Default OutboundProvider when no API key is configured.

Returns an empty list. Used by the orchestrator so callers can stay uniform
across the configured / unconfigured states without `if provider is None`.
"""

from __future__ import annotations

from src.application.cold_outreach.base import Contact


class NullProvider:
    name: str = "null"

    async def find_contacts(self, domain: str, *, limit: int = 1) -> list[Contact]:
        # Intentionally a no-op — never logs at info to avoid spamming when
        # the worker idles waiting for a provider key.
        return []
