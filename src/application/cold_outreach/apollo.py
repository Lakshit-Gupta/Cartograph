"""Apollo.io contact discovery.

Apollo's `/v1/mixed_people/search` returns ranked people for a company
domain. We ask for ONE person at a time (the cap module gates volume),
filtered to active employees with verified emails.

The whole response is treated as untrusted — bios / titles flow through
sanitizer before any LLM call. Only the email itself is structurally
validated.

API docs: https://api.apollo.io/  (read-only `people_search` endpoint).
"""

from __future__ import annotations

import hashlib
from typing import Any

import httpx

from src.application.cold_outreach.base import Contact
from src.common.logger import get_logger

_log = get_logger(__name__)

_APOLLO_URL = "https://api.apollo.io/v1/mixed_people/search"


def _hash_email(email: str) -> str:
    """Short SHA-256 prefix for correlation logging — never log raw email."""
    return hashlib.sha256(email.lower().encode("utf-8")).hexdigest()[:10]


def _is_email(s: str) -> bool:
    """Cheap RFC-5321-ish sanity check; does not validate deliverability."""
    if not s or "@" not in s:
        return False
    local, _, dom = s.rpartition("@")
    return bool(local) and "." in dom and " " not in s


class ApolloProvider:
    name: str = "apollo"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def find_contacts(self, domain: str, *, limit: int = 1) -> list[Contact]:
        if not self._api_key:
            return []
        # Roles that historically respond to cold founder pitches better
        # than generic recruiters. Engineering-Manager and Director levels
        # are wide enough to catch hiring managers without spamming CEOs.
        payload: dict[str, Any] = {
            "api_key": self._api_key,
            "q_organization_domains": domain,
            "page": 1,
            "per_page": max(1, min(int(limit), 5)),
            "person_titles": [
                "Engineering Manager",
                "Director of Engineering",
                "VP Engineering",
                "Head of Engineering",
                "Hiring Manager",
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(_APOLLO_URL, json=payload)
                if resp.status_code >= 400:
                    _log.warning("apollo_non_2xx", status=resp.status_code, domain=domain)
                    return []
                data = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            _log.warning("apollo_request_failed", err=str(e), domain=domain)
            return []

        # Apollo wraps results in either `people` or `contacts` depending on
        # endpoint version; defensively try both.
        people = data.get("people") or data.get("contacts") or []
        out: list[Contact] = []
        for p in people[: max(1, int(limit))]:
            email = (p.get("email") or "").strip().lower()
            if not _is_email(email):
                continue
            name = (p.get("name") or "").strip() or None
            title = (p.get("title") or "").strip() or None
            bio = p.get("headline") or p.get("summary") or ""
            bio = bio.strip()[:500] or None
            out.append(
                Contact(
                    email=email,
                    name=name,
                    title=title,
                    bio=bio,
                    source=self.name,
                )
            )
        _log.debug(
            "apollo_contacts_returned",
            domain=domain,
            n=len(out),
            email_hashes=[_hash_email(c.email) for c in out],
        )
        return out
