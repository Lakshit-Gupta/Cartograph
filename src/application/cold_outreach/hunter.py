"""Hunter.io contact discovery.

Hunter's `/v2/domain-search` returns published emails for a company domain.
Free tier allows ~25 requests/month — cheap for our 10-emails/day ceiling.

API docs: https://hunter.io/api-documentation/v2#domain-search
"""

from __future__ import annotations

import hashlib

import httpx

from src.application.cold_outreach.base import Contact
from src.common.logger import get_logger

_log = get_logger(__name__)

_HUNTER_URL = "https://api.hunter.io/v2/domain-search"


def _hash_email(email: str) -> str:
    return hashlib.sha256(email.lower().encode("utf-8")).hexdigest()[:10]


def _is_email(s: str) -> bool:
    if not s or "@" not in s:
        return False
    local, _, dom = s.rpartition("@")
    return bool(local) and "." in dom and " " not in s


class HunterProvider:
    name: str = "hunter"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def find_contacts(self, domain: str, *, limit: int = 1) -> list[Contact]:
        if not self._api_key:
            return []
        params: dict[str, str] = {
            "domain": domain,
            "api_key": self._api_key,
            "limit": str(max(1, min(int(limit), 10))),
            # Filter to addresses Hunter has actually seen used publicly.
            "type": "personal",
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(_HUNTER_URL, params=params)
                if resp.status_code >= 400:
                    _log.warning("hunter_non_2xx", status=resp.status_code, domain=domain)
                    return []
                data = resp.json()
        except (httpx.HTTPError, ValueError) as err:
            _log.warning("hunter_request_failed", err=str(err), domain=domain)
            return []

        emails = (data.get("data") or {}).get("emails") or []
        out: list[Contact] = []
        for entry in emails[: max(1, int(limit))]:
            email = (entry.get("value") or "").strip().lower()
            if not _is_email(email):
                continue
            first = (entry.get("first_name") or "").strip()
            last = (entry.get("last_name") or "").strip()
            full = " ".join(p for p in (first, last) if p) or None
            title = (entry.get("position") or "").strip() or None
            # Hunter doesn't return a bio; we leave it None.
            out.append(
                Contact(
                    email=email,
                    name=full,
                    title=title,
                    bio=None,
                    source=self.name,
                )
            )
        _log.debug(
            "hunter_contacts_returned",
            domain=domain,
            n=len(out),
            email_hashes=[_hash_email(c.email) for c in out],
        )
        return out
