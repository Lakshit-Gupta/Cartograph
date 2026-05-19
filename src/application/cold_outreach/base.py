"""Protocol surface for outbound contact-discovery providers.

Apollo and Hunter both wrap this Protocol so the orchestrator (`sender.py`)
never branches on provider identity. NullProvider returns [] so the
worker boots gracefully when no provider keys are configured.

Contact is intentionally minimal — `bio` is untrusted text and must pass
through `sanitizer.scrub_text` before it ever reaches the LLM drafter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Contact:
    """A single person discovered for a company.

    Attributes:
        email: the address we'll send to. Lowercased downstream.
        name: full name. May be None when only a generic role-mailbox is known.
        title: job title at the company (untrusted; pass through sanitizer).
        bio: short description; UNTRUSTED — sanitize before LLM use.
        source: provider slug ("apollo" / "hunter" / "null") for audit.
    """

    email: str
    name: str | None
    title: str | None
    bio: str | None
    source: str


class OutboundProvider(Protocol):
    """Discover up to N contacts for a company domain.

    Implementations:
      - apollo.ApolloProvider     (paid; requires APOLLO_API_KEY)
      - hunter.HunterProvider     (paid; requires HUNTER_API_KEY)
      - null_provider.NullProvider (always returns []; default when no
                                    keys are configured)
    """

    name: str

    async def find_contacts(self, domain: str, *, limit: int = 1) -> list[Contact]:
        """Return ranked contacts for `domain`. Never raises — empty on error."""
        ...
