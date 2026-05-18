"""Tests for the Stage 2.2 identity wiring through the fetcher pipeline.

These tests pin the contract between the crawler and the identity vault:
  - FetchRequest stays backward-compat (cookies/ua_string default None)
  - HttpFetcher threads leased cookies through curl_cffi's cookies= kwarg
  - HttpFetcher's UA override wins over the cf_clearance UA
  - Crawler falls through to anonymous fetch when the vault is empty
  - Crawler marks the leased identity banned on a 403 from an auth source

The vault is empty in production today, so the fall-through path is the
steady state until sock-puppets are seeded. The ban-marking path is what
keeps a single bad cookie from sinking every sibling identity for hours.

curl_cffi can't be intercepted by respx (it uses libcurl, not httpx), so
we monkeypatch `curl_cffi.requests.AsyncSession` directly. respx is in
the dev dependency group only for httpx-backed paths (FlareSolverr).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.common.types import IdentityLease
from src.fetchers.base import FetchRequest, FetchResponse

# ---------------------------------------------------------------------------
# 1. Dataclass backward-compat
# ---------------------------------------------------------------------------


@pytest.mark.smoke
def test_fetchrequest_default_cookies_and_ua_none() -> None:
    """Every existing crawler call site builds FetchRequest with no identity
    fields. Defaults MUST stay None — flipping them to `{}`/`""` would change
    the http.py code path that only fires when the field is truthy."""
    req = FetchRequest(source_id=1, source_slug="ats_greenhouse", url="https://x/")
    assert req.cookies is None
    assert req.ua_string is None
    # Existing fields still default as before
    assert req.method == "GET"
    assert req.identity_id is None
    assert req.timeout_s == 30.0


@pytest.mark.smoke
def test_fetchrequest_accepts_identity_context() -> None:
    """When the crawler splices identity context, both fields land verbatim."""
    cookies = {"session": "abc", "csrf": "xyz"}
    ua = "Mozilla/5.0 (X11; Linux x86_64) Chrome/131"
    req = FetchRequest(
        source_id=42,
        source_slug="contra",
        url="https://contra.com/jobs",
        identity_id=7,
        cookies=cookies,
        ua_string=ua,
    )
    assert req.cookies == cookies
    assert req.ua_string == ua
    assert req.identity_id == 7


# ---------------------------------------------------------------------------
# 2. HttpFetcher threads cookies + UA through curl_cffi
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Stand-in for curl_cffi.requests.Response with just the surface
    HttpFetcher reads. Real curl_cffi never gets invoked because the test
    swaps AsyncSession with this fake."""

    def __init__(self, *, status_code: int = 200, body: str = "<html>ok</html>") -> None:
        self.status_code = status_code
        self.text = body
        self.content = body.encode()
        self.headers = {"content-type": "text/html"}


class _FakeAsyncSession:
    """Records every kwarg passed to .request so tests can assert the
    cookies + headers handoff. Returns a fixed _FakeResponse."""

    last: dict[str, Any] | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        type(self).init_kwargs = kwargs

    async def __aenter__(self) -> _FakeAsyncSession:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def request(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
        type(self).last = {"method": method, "url": url, **kwargs}
        return _FakeResponse()


@pytest.fixture()
def patch_async_session(monkeypatch: pytest.MonkeyPatch) -> type[_FakeAsyncSession]:
    """Swap AsyncSession with a recording fake AND short-circuit the DB
    clearance lookup so the fetcher doesn't need an asyncpg pool."""
    monkeypatch.setattr("src.fetchers.http.AsyncSession", _FakeAsyncSession)

    async def _no_clearance(*_args: Any, **_kwargs: Any) -> tuple[None, None]:
        return None, None

    async def _no_bump(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr("src.fetchers.http.HttpFetcher._load_clearance", _no_clearance)
    monkeypatch.setattr("src.fetchers.http.HttpFetcher._bump_clearance", _no_bump)
    _FakeAsyncSession.last = None
    return _FakeAsyncSession


@pytest.mark.asyncio
async def test_http_fetcher_passes_cookies_to_session(patch_async_session: type[_FakeAsyncSession]) -> None:
    """req.cookies must reach curl_cffi as the `cookies=` kwarg verbatim, not
    as a flattened Cookie header (which would collide with the cf_clearance
    cookie path)."""
    from src.fetchers.http import HttpFetcher

    req = FetchRequest(
        source_id=11,
        source_slug="contra",
        url="https://contra.example/jobs",
        cookies={"session": "leased-token-abc"},
    )
    resp = await HttpFetcher().fetch(req)
    assert resp.status == 200
    assert patch_async_session.last is not None
    assert patch_async_session.last["cookies"] == {"session": "leased-token-abc"}


@pytest.mark.asyncio
async def test_http_fetcher_overrides_ua_when_set(patch_async_session: type[_FakeAsyncSession]) -> None:
    """Leased ua_string MUST overwrite anything in req.headers — origin
    session trackers pin to the sock-puppet's fingerprint, the cf_clearance
    UA is only valid against Cloudflare."""
    from src.fetchers.http import HttpFetcher

    req = FetchRequest(
        source_id=11,
        source_slug="cuvette",
        url="https://cuvette.example/internships",
        headers={"User-Agent": "should-be-overridden"},
        ua_string="Mozilla/5.0 (sock-puppet-fingerprint) Chrome/131",
    )
    resp = await HttpFetcher().fetch(req)
    assert resp.status == 200
    assert patch_async_session.last is not None
    sent_headers = patch_async_session.last["headers"]
    assert sent_headers["User-Agent"] == "Mozilla/5.0 (sock-puppet-fingerprint) Chrome/131"


@pytest.mark.asyncio
async def test_http_fetcher_no_cookies_when_unset(patch_async_session: type[_FakeAsyncSession]) -> None:
    """When req.cookies is None, the cookies= kwarg goes through as None so
    curl_cffi falls back to its own cookie jar (per-session, empty)."""
    from src.fetchers.http import HttpFetcher

    req = FetchRequest(source_id=11, source_slug="ats_greenhouse", url="https://example.com/jobs")
    await HttpFetcher().fetch(req)
    assert patch_async_session.last is not None
    assert patch_async_session.last["cookies"] is None


# ---------------------------------------------------------------------------
# 3. Crawler — fall-through when vault empty
# ---------------------------------------------------------------------------


def _fake_response(status: int = 200, body: str = "<html>ok</html>") -> FetchResponse:
    return FetchResponse(
        status=status,
        body=body,
        content_type="text/html",
        tier=0,
        headers={},
        error=None,
    )


class _FakeQ:
    """Minimal RedisQ stand-in — records every publish/dlq for assertion."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, stream: str, payload: dict[str, Any]) -> str:
        self.published.append((stream, payload))
        return "msg-0"

    async def dlq(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def ack(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _FakeDispatcher:
    """Records the FetchRequest it sees and returns a canned FetchResponse."""

    def __init__(self, resp: FetchResponse) -> None:
        self._resp = resp
        self.calls: list[FetchRequest] = []

    async def fetch(self, req: FetchRequest, tier_chain: list[int]) -> FetchResponse:
        self.calls.append(req)
        return self._resp


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_crawler_falls_through_when_vault_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """`identity_vault.checkout` returns None when no healthy identity is
    available — the steady state today since the vault is empty. The crawler
    MUST still issue the fetch, log `identity_lease_missed`, and never
    crash. This is the contract that keeps the pipeline running until
    sock-puppets are seeded."""
    from src.workers import crawler as crawler_mod

    # Auth-required source: lookup returns a platform name.
    async def fake_platform(_source_id: int) -> str | None:
        return "contra"

    # Vault empty — checkout returns None.
    async def fake_checkout(**_kwargs: Any) -> IdentityLease | None:
        return None

    release_calls: list[int] = []

    async def fake_release(lease_id: int) -> None:
        release_calls.append(lease_id)

    async def fake_ban(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("mark_banned must not fire when no lease was issued")

    monkeypatch.setattr(crawler_mod, "_lookup_auth_platform", fake_platform)
    monkeypatch.setattr(crawler_mod.identity_vault, "checkout", fake_checkout)
    monkeypatch.setattr(crawler_mod.identity_vault, "release", fake_release)
    monkeypatch.setattr(crawler_mod.identity_vault, "mark_banned", fake_ban)

    q = _FakeQ()
    disp = _FakeDispatcher(_fake_response(200))
    fields = {
        "source_id": 11,
        "source_slug": "contra",
        "url": "https://contra.example/jobs",
        "crawler_strategy": "contra_session",
        "tier_chain": [0],
        "requires_identity": True,
        "correlation_id": "corr-1",
    }

    await crawler_mod._process(q, disp, fields)

    # Fetch fired despite no identity lease.
    assert len(disp.calls) == 1
    sent = disp.calls[0]
    assert sent.cookies is None
    assert sent.ua_string is None
    assert sent.identity_id is None

    # No release attempted (no lease to release).
    assert release_calls == []

    # FetchResult landed on the EXTRACT stream.
    assert len(q.published) == 1
    stream, payload = q.published[0]
    assert stream == "stream:extract"
    assert payload["correlation_id"] == "corr-1"


@pytest.mark.asyncio
async def test_crawler_skips_checkout_for_anonymous_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sources without `auth_account_id` MUST NOT call checkout at all — saves
    a DB round-trip and avoids leasing identities for ATS sources that should
    stay anonymous."""
    from src.workers import crawler as crawler_mod

    async def fake_platform(_source_id: int) -> str | None:
        return None  # anonymous source

    async def fake_checkout(**_kwargs: Any) -> IdentityLease | None:
        raise AssertionError("checkout must not be called for anonymous sources")

    monkeypatch.setattr(crawler_mod, "_lookup_auth_platform", fake_platform)
    monkeypatch.setattr(crawler_mod.identity_vault, "checkout", fake_checkout)

    q = _FakeQ()
    disp = _FakeDispatcher(_fake_response(200))
    fields = {
        "source_id": 99,
        "source_slug": "ats_greenhouse",
        "url": "https://boards-api.greenhouse.io/v1/x/jobs",
        "crawler_strategy": "ats_greenhouse",
        "tier_chain": [0],
        "requires_identity": False,
        "correlation_id": "corr-2",
    }

    await crawler_mod._process(q, disp, fields)
    assert len(disp.calls) == 1
    assert disp.calls[0].cookies is None


# ---------------------------------------------------------------------------
# 4. Crawler — mark_banned on identity-ban signal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crawler_marks_banned_on_403_for_auth_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the dispatcher returns 403 (without a CF marker) on an auth-gated
    source AND we hold a lease, the identity must be flipped to ban_status =
    banned. This prevents the next checkout from re-handing the same dead
    cookie to another worker."""
    from src.workers import crawler as crawler_mod

    async def fake_platform(_source_id: int) -> str | None:
        return "contra"

    fake_lease = IdentityLease(
        identity_id=77,
        platform="contra",
        cookies={"session": "stale-token"},
        ua_string="Mozilla/5.0 Test",
        lease_id=999,
        expires_at=datetime.now(UTC) + timedelta(seconds=600),
    )

    async def fake_checkout(**_kwargs: Any) -> IdentityLease:
        return fake_lease

    release_calls: list[int] = []
    ban_calls: list[tuple[int, str]] = []

    async def fake_release(lease_id: int) -> None:
        release_calls.append(lease_id)

    async def fake_ban(identity_id: int, reason: str) -> None:
        ban_calls.append((identity_id, reason))

    monkeypatch.setattr(crawler_mod, "_lookup_auth_platform", fake_platform)
    monkeypatch.setattr(crawler_mod.identity_vault, "checkout", fake_checkout)
    monkeypatch.setattr(crawler_mod.identity_vault, "release", fake_release)
    monkeypatch.setattr(crawler_mod.identity_vault, "mark_banned", fake_ban)

    q = _FakeQ()
    # 403 with no CF marker — identity ban signal.
    disp = _FakeDispatcher(_fake_response(403, body="<html>Forbidden</html>"))
    fields = {
        "source_id": 11,
        "source_slug": "contra",
        "url": "https://contra.example/jobs",
        "crawler_strategy": "contra_session",
        "tier_chain": [0],
        "requires_identity": True,
        "correlation_id": "corr-3",
    }

    await crawler_mod._process(q, disp, fields)

    # mark_banned fired exactly once with the leased identity.
    assert ban_calls == [(77, "status_403")]
    # Lease always releases.
    assert release_calls == [999]
    # Cookies + UA were spliced in.
    assert len(disp.calls) == 1
    assert disp.calls[0].cookies == {"session": "stale-token"}
    assert disp.calls[0].ua_string == "Mozilla/5.0 Test"
    assert disp.calls[0].identity_id == 77


@pytest.mark.asyncio
async def test_crawler_does_not_mark_banned_on_cf_403(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 403 from Cloudflare (body contains 'Attention Required') is NOT an
    identity ban — it's a route-layer interstitial that tier escalation
    handles. Mark-banning the identity here would burn a healthy account
    every time CF flips. Regression guard for the heuristic."""
    from src.workers import crawler as crawler_mod

    async def fake_platform(_source_id: int) -> str | None:
        return "contra"

    fake_lease = IdentityLease(
        identity_id=42,
        platform="contra",
        cookies={},
        ua_string=None,
        lease_id=100,
        expires_at=datetime.now(UTC) + timedelta(seconds=600),
    )

    async def fake_checkout(**_kwargs: Any) -> IdentityLease:
        return fake_lease

    ban_calls: list[Any] = []

    async def fake_ban(*args: Any, **kwargs: Any) -> None:
        ban_calls.append((args, kwargs))

    monkeypatch.setattr(crawler_mod, "_lookup_auth_platform", fake_platform)
    monkeypatch.setattr(crawler_mod.identity_vault, "checkout", fake_checkout)
    monkeypatch.setattr(crawler_mod.identity_vault, "release", AsyncMock())
    monkeypatch.setattr(crawler_mod.identity_vault, "mark_banned", fake_ban)

    q = _FakeQ()
    cf_403 = FetchResponse(
        status=403,
        body="<html>Attention Required! | Cloudflare</html>",
        content_type="text/html",
        tier=0,
        headers={},
        error="status_403_cf_True",
        cf_challenge_observed=True,
    )
    disp = _FakeDispatcher(cf_403)
    fields = {
        "source_id": 11,
        "source_slug": "contra",
        "url": "https://contra.example/jobs",
        "crawler_strategy": "contra_session",
        "tier_chain": [0],
        "requires_identity": True,
        "correlation_id": "corr-4",
    }

    await crawler_mod._process(q, disp, fields)
    assert ban_calls == []


@pytest.mark.asyncio
async def test_crawler_marks_banned_on_explicit_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """Some origins (Reddit shadowban responses) set X-Identity-Banned
    explicitly. That bypasses the CF heuristic — always treat as a ban."""
    from src.workers import crawler as crawler_mod

    fake_lease = IdentityLease(
        identity_id=55,
        platform="reddit",
        cookies={"session": "x"},
        ua_string=None,
        lease_id=200,
        expires_at=datetime.now(UTC) + timedelta(seconds=600),
    )

    async def fake_platform(_source_id: int) -> str | None:
        return "reddit"

    async def fake_checkout(**_kwargs: Any) -> IdentityLease:
        return fake_lease

    ban_calls: list[tuple[int, str]] = []

    async def fake_ban(identity_id: int, reason: str) -> None:
        ban_calls.append((identity_id, reason))

    monkeypatch.setattr(crawler_mod, "_lookup_auth_platform", fake_platform)
    monkeypatch.setattr(crawler_mod.identity_vault, "checkout", fake_checkout)
    monkeypatch.setattr(crawler_mod.identity_vault, "release", AsyncMock())
    monkeypatch.setattr(crawler_mod.identity_vault, "mark_banned", fake_ban)

    q = _FakeQ()
    # 200 OK but explicit ban header.
    resp = FetchResponse(
        status=200,
        body="<html>ok-ish</html>",
        content_type="text/html",
        tier=0,
        headers={"X-Identity-Banned": "true"},
        error=None,
    )
    disp = _FakeDispatcher(resp)
    fields = {
        "source_id": 11,
        "source_slug": "reddit_forhire",
        "url": "https://reddit.example/r/forhire",
        "crawler_strategy": "reddit_forhire",
        "tier_chain": [0],
        "requires_identity": True,
        "correlation_id": "corr-5",
    }

    await crawler_mod._process(q, disp, fields)
    assert ban_calls == [(55, "header:X-Identity-Banned")]


# ---------------------------------------------------------------------------
# 5. Camoufox cookie translation helper
# ---------------------------------------------------------------------------


def test_camoufox_cookies_to_playwright_uses_url_form() -> None:
    """The Playwright `add_cookies` shape requires either url or domain. We
    chose url-form so we don't have to handle leading-dot domain edge cases
    (Playwright derives the right scope automatically). Regression guard so
    a future refactor doesn't accidentally switch to domain-form and break
    cookie scoping for subdomains."""
    from src.fetchers.browser.camoufox import _cookies_to_playwright

    out = _cookies_to_playwright(
        {"sessionid": "abc", "csrf": "xyz"},
        "https://contra.example/jobs/123",
    )
    assert len(out) == 2
    names = {c["name"] for c in out}
    assert names == {"sessionid", "csrf"}
    for c in out:
        assert c["url"] == "https://contra.example"
        assert "value" in c


def test_camoufox_cookies_to_playwright_empty_input() -> None:
    from src.fetchers.browser.camoufox import _cookies_to_playwright

    assert _cookies_to_playwright({}, "https://x.example/") == []


# ---------------------------------------------------------------------------
# 6. Ban-signal heuristic — direct test of the predicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "body", "headers", "expected_banned", "expected_reason"),
    [
        # Clean 200 — never a ban.
        (200, "<html>ok</html>", {}, False, ""),
        # 403 + CF body → CF interstitial, NOT a ban.
        (403, "Attention Required! Cloudflare", {}, False, ""),
        (403, "Just a moment...", {}, False, ""),
        # 403 without CF marker → identity ban.
        (403, "<html>Forbidden</html>", {}, True, "status_403"),
        # 401 → ban (credentials rejected).
        (401, "Unauthorized", {}, True, "status_401"),
        # Explicit header dominates.
        (200, "ok", {"X-Identity-Banned": "true"}, True, "header:X-Identity-Banned"),
        # 5xx is not a ban — backend hiccup, retry later.
        (502, "Bad Gateway", {}, False, ""),
    ],
)
def test_is_ban_signal_heuristic(
    status: int,
    body: str,
    headers: dict[str, str],
    expected_banned: bool,
    expected_reason: str,
) -> None:
    from src.workers.crawler import _is_ban_signal

    resp = FetchResponse(
        status=status,
        body=body,
        content_type="text/html",
        tier=0,
        headers=headers,
        error=None,
        cf_challenge_observed=False,
    )
    banned, reason = _is_ban_signal(resp)
    assert banned is expected_banned
    assert reason == expected_reason


# Silence ruff RUF for the unused MagicMock import (kept available for future
# test additions that need broader patching).
_ = MagicMock
