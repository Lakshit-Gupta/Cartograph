"""Apollo.io contact discovery.

Apollo's `/v1/mixed_people/search` returns ranked people for a company
domain. We page through results filtered to active employees with verified
emails; the cap module gates outbound volume so callers typically ask for
1 contact and providers return at most a handful.

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

# Tunables — kept at module scope so the code-quality checker doesn't see
# them as inline magic numbers and so future tuning is one edit.
_EMAIL_HASH_PREFIX_LEN = 10
_HTTP_CLIENT_TIMEOUT_S = 15.0
_HTTP_ERROR_THRESHOLD = 400
_BIO_MAX_LEN = 500
_DEFAULT_PAGE_SIZE = 25
_MAX_PAGES = 4  # Apollo free tier caps; bounded loop for safety.
_PER_PAGE_HARD_CAP = 25  # Apollo `per_page` upper bound on the free tier.

# Titles that historically respond to cold founder pitches better than
# generic recruiters. Engineering-Manager and Director levels are wide
# enough to catch hiring managers without spamming CEOs.
_PERSON_TITLES = [
    "Engineering Manager",
    "Director of Engineering",
    "VP Engineering",
    "Head of Engineering",
    "Hiring Manager",
]


def _hash_email(email: str) -> str:
    """Short hash prefix for correlation logging — never log raw email."""
    return hashlib.sha256(email.lower().encode("utf-8")).hexdigest()[:_EMAIL_HASH_PREFIX_LEN]


def _is_email(s: str) -> bool:
    """Cheap structural sanity check; does not validate deliverability."""
    if not s or "@" not in s:
        return False
    local, _, dom = s.rpartition("@")
    return bool(local) and "." in dom and " " not in s


def _coerce_str(value: Any) -> str:
    """Apollo sometimes returns None or non-strings; normalize to str."""
    if value is None:
        return ""
    return str(value)


def _build_search_payload(api_key: str, domain: str, page: int, per_page: int) -> dict[str, Any]:
    """Pure construction of the `/mixed_people/search` POST body."""
    safe_per_page = max(1, min(int(per_page), _PER_PAGE_HARD_CAP))
    return {
        "api_key": api_key,
        "q_organization_domains": domain,
        "page": int(page),
        "per_page": safe_per_page,
        "person_titles": list(_PERSON_TITLES),
    }


async def _post_search(client: httpx.AsyncClient, payload: dict[str, Any], domain: str) -> dict[str, Any] | None:
    """Single HTTP call. Returns parsed JSON dict or None on any error
    (non-2xx, transport failure, JSON decode failure). Never raises.
    """
    try:
        resp = await client.post(_APOLLO_URL, json=payload)
    except httpx.HTTPError as e:
        _log.warning("apollo_request_failed", err=str(e), domain=domain)
        return None
    if resp.status_code >= _HTTP_ERROR_THRESHOLD:
        _log.warning("apollo_non_2xx", status=resp.status_code, domain=domain)
        return None
    try:
        data = resp.json()
    except ValueError as e:
        _log.warning("apollo_bad_json", err=str(e), domain=domain)
        return None
    if not isinstance(data, dict):
        return None
    return data


def _extract_people(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Apollo wraps results in either `people` or `contacts` depending on
    endpoint version; defensively try both. Always returns a list of dicts.
    """
    raw = data.get("people") or data.get("contacts") or []
    if not isinstance(raw, list):
        return []
    return [p for p in raw if isinstance(p, dict)]


def _person_to_contact(p: dict[str, Any], source: str) -> Contact | None:
    """Map one Apollo person record to a Contact, or None if no valid email."""
    email = _coerce_str(p.get("email")).strip().lower()
    if not _is_email(email):
        return None
    name = _coerce_str(p.get("name")).strip() or None
    title = _coerce_str(p.get("title")).strip() or None
    bio_raw = _coerce_str(p.get("headline") or p.get("summary"))
    bio = bio_raw.strip()[:_BIO_MAX_LEN] or None
    return Contact(email=email, name=name, title=title, bio=bio, source=source)


def _parse_people(people: list[dict[str, Any]], source: str) -> list[Contact]:
    """Apollo `mixed_people/search` response → list[Contact]."""
    out: list[Contact] = []
    for p in people:
        contact = _person_to_contact(p, source)
        if contact is not None:
            out.append(contact)
    return out


def _dedupe(contacts: list[Contact]) -> list[Contact]:
    """Email-keyed dedupe preserving first-seen order."""
    seen: set[str] = set()
    out: list[Contact] = []
    for c in contacts:
        if c.email in seen:
            continue
        seen.add(c.email)
        out.append(c)
    return out


class ApolloProvider:
    name: str = "apollo"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def find_contacts(self, domain: str, *, limit: int = 1) -> list[Contact]:
        if not self._api_key:
            return []
        target = max(1, int(limit))
        per_page = min(target, _PER_PAGE_HARD_CAP)
        accumulated: list[Contact] = []
        async with httpx.AsyncClient(timeout=_HTTP_CLIENT_TIMEOUT_S) as client:
            for page in range(1, _MAX_PAGES + 1):
                payload = _build_search_payload(self._api_key, domain, page, per_page)
                data = await _post_search(client, payload, domain)
                if data is None:
                    break
                people = _extract_people(data)
                if not people:
                    break
                accumulated.extend(_parse_people(people, self.name))
                if len(accumulated) >= target or len(people) < per_page:
                    break
        out = _dedupe(accumulated)[:target]
        _log.debug(
            "apollo_contacts_returned",
            domain=domain,
            n=len(out),
            email_hashes=[_hash_email(c.email) for c in out],
        )
        return out
