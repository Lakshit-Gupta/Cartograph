"""Contract tests for the api-service → PostgREST reverse proxy (Phase 5.2).

These tests are hermetic — no live PostgREST and no live Postgres. The
upstream PostgREST is mocked with `respx` (httpx test recorder); the
proxy itself is mounted on a one-off `FastAPI()` so we never touch the
`lifespan` of `src.api.main:app` (which would try to open a real
asyncpg pool).

The contract under test is the one published in
`docs/runbooks/dashboard.md`:

  - Backend agent ships `src/api/postgrest_proxy.py` with an
    `APIRouter` exposed as `router`, prefix `/api/postgrest`.
  - The proxy forwards **GET / HEAD / OPTIONS** to
    `http://postgrest:3000/<path>` and preserves the query string.
  - **POST / PUT / DELETE / PATCH** are refused with `405 Method Not
    Allowed`. The dashboard is read-only by design (CLAUDE.md Phase
    5.2 SHIPPED entry); there is no write path through the browser.
  - Client-supplied `Authorization` and `Cookie` headers MUST be
    stripped before forwarding so the browser cannot smuggle
    elevated credentials past the trusted server-set role.
  - Upstream status codes (200, 404, 401, …) and JSON bodies are
    relayed verbatim to the client.
  - If PostgREST is unreachable the proxy returns a gateway-error
    status (502 / 503 / 504) and **never hangs** the caller.

If the backend agent has not yet landed the proxy, every test in this
file fails fast with `ImportError` raised inside `_proxy_router` —
that is the intended behaviour. The contract is the source of truth.
"""

from __future__ import annotations

import importlib

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# The proxy is expected to forward to this upstream host:port from inside the
# Docker network (PostgREST listens on 3000 by convention; no host port
# mapping per CLAUDE.md and `docs/runbooks/dashboard.md`).
_UPSTREAM_BASE = "http://postgrest:3000"


def _proxy_router():
    """Import the backend agent's proxy router lazily so test collection
    surfaces a clear error if the backend half of Phase 5.2 has not landed
    yet. Raises ImportError with an actionable message when missing."""
    try:
        mod = importlib.import_module("src.api.postgrest_proxy")
    except ModuleNotFoundError as exc:  # pragma: no cover — failure path
        raise ImportError(
            "Phase 5.2 backend not landed yet: src.api.postgrest_proxy is "
            "missing. See docs/runbooks/dashboard.md for the contract these "
            "tests pin."
        ) from exc
    router = getattr(mod, "router", None)
    if router is None:  # pragma: no cover — failure path
        raise ImportError(
            "src.api.postgrest_proxy imported but exports no `router` "
            "attribute. The contract expects an APIRouter with prefix "
            "'/api/postgrest'."
        )
    return router


@pytest.fixture
def app() -> FastAPI:
    """One-off FastAPI app that mounts ONLY the proxy router.

    Avoids `src.api.main:app`'s lifespan (which opens a real Postgres pool).
    """
    app = FastAPI()
    app.include_router(_proxy_router())
    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    # raise_server_exceptions=False so a 5xx surfaces as a response (matching
    # what a real browser would see) rather than re-raising into the test.
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# 1. Write verbs are refused — read-only contract.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
@pytest.mark.smoke
def test_write_methods_rejected_with_405(client: TestClient, method: str) -> None:
    """The dashboard is read-only. Any write verb against the proxy MUST
    fail closed with 405, NEVER reach PostgREST.

    A leaked write path would let a same-origin script (or an XSS in a
    future Cartograph asset) flip `sources.status`, delete `applications`,
    etc. through `pgrst_anon`. The role's GRANTs are the second line of
    defence; the proxy is the first.
    """
    with respx.mock(assert_all_called=False) as r:
        upstream = r.route(host="postgrest")
        resp = client.request(method, "/api/postgrest/v_overview", json={"x": 1})
    assert resp.status_code == 405, f"{method} must be refused with 405 (was {resp.status_code} {resp.text!r})"
    assert upstream.called is False, f"{method} reached upstream PostgREST — read-only contract violated."


# ---------------------------------------------------------------------------
# 2. GET / HEAD / OPTIONS forward to upstream with query string preserved.
# ---------------------------------------------------------------------------


@pytest.mark.smoke
def test_get_forwards_to_upstream_with_query_string(client: TestClient) -> None:
    """`GET /api/postgrest/v_costs?days=7&order=date.desc` must land at
    `GET http://postgrest:3000/v_costs?days=7&order=date.desc` (exact
    query string preserved) and the upstream's JSON body must come back
    verbatim."""
    payload = [{"date": "2026-05-19", "usd": 0.12}]
    with respx.mock(assert_all_called=False) as r:
        route = r.get(f"{_UPSTREAM_BASE}/v_costs").respond(
            status_code=200,
            json=payload,
        )
        resp = client.get(
            "/api/postgrest/v_costs",
            params={"days": "7", "order": "date.desc"},
        )

    assert resp.status_code == 200
    assert resp.json() == payload
    assert route.called is True
    # Verify the forwarded URL preserved the query string verbatim.
    forwarded_url = route.calls.last.request.url
    assert forwarded_url.path == "/v_costs"
    assert dict(forwarded_url.params) == {"days": "7", "order": "date.desc"}


def test_head_method_forwarded(client: TestClient) -> None:
    """HEAD is a legitimate read verb (browsers + PostgREST itself use it
    for cache validation). The proxy must allow it through."""
    with respx.mock(assert_all_called=False) as r:
        route = r.head(f"{_UPSTREAM_BASE}/v_overview").respond(status_code=200)
        resp = client.head("/api/postgrest/v_overview")
    assert resp.status_code == 200
    assert route.called is True


def test_options_method_forwarded(client: TestClient) -> None:
    """OPTIONS preflight (PostgREST replies with its own CORS headers).
    The proxy must allow it. CORS doesn't matter for same-origin requests
    in production, but PostgREST clients may still send OPTIONS for
    capability discovery."""
    with respx.mock(assert_all_called=False) as r:
        route = r.options(f"{_UPSTREAM_BASE}/v_overview").respond(status_code=200)
        resp = client.options("/api/postgrest/v_overview")
    # Some FastAPI configs surface OPTIONS as 200 / 204 — both acceptable.
    assert resp.status_code in (200, 204), resp.status_code
    assert route.called is True


# ---------------------------------------------------------------------------
# 3. Dangerous client headers are stripped before forwarding.
# ---------------------------------------------------------------------------


@pytest.mark.smoke
def test_client_authorization_header_stripped(client: TestClient) -> None:
    """A client-set `Authorization` header MUST NOT reach PostgREST.

    PostgREST treats Authorization as the JWT for role switching. If the
    browser could set it, anyone same-origin could escalate from
    `pgrst_anon` to any role they can name. The proxy strips it; if the
    deployment ever introduces JWT, the proxy is the only path that may
    set it (server-side, from SOPS).
    """
    with respx.mock(assert_all_called=False) as r:
        route = r.get(f"{_UPSTREAM_BASE}/v_overview").respond(json=[])
        resp = client.get(
            "/api/postgrest/v_overview",
            headers={"Authorization": "Bearer attacker-jwt"},
        )
    assert resp.status_code == 200
    assert route.called is True
    forwarded_headers = route.calls.last.request.headers
    # Header may legitimately be re-set by the server (e.g. a SOPS-loaded
    # service token); what we forbid is the client's value reaching upstream.
    forwarded_auth = forwarded_headers.get("authorization", "")
    assert "attacker-jwt" not in forwarded_auth, f"client Authorization leaked upstream: {forwarded_auth!r}"


def test_client_cookie_header_stripped(client: TestClient) -> None:
    """Client `Cookie` header MUST NOT reach PostgREST. Same defence as
    Authorization: PostgREST has no concept of browser sessions; any
    cookie value reaching it is at best ignored and at worst exposes a
    parsing surface we don't audit."""
    with respx.mock(assert_all_called=False) as r:
        route = r.get(f"{_UPSTREAM_BASE}/v_overview").respond(json=[])
        resp = client.get(
            "/api/postgrest/v_overview",
            headers={"Cookie": "session=stolen-token; other=abc"},
        )
    assert resp.status_code == 200
    assert route.called is True
    forwarded_cookie = route.calls.last.request.headers.get("cookie", "")
    assert "stolen-token" not in forwarded_cookie, f"client Cookie leaked upstream: {forwarded_cookie!r}"


# ---------------------------------------------------------------------------
# 4. Upstream status + body relayed verbatim.
# ---------------------------------------------------------------------------


@pytest.mark.smoke
def test_proxy_relays_200_json_verbatim(client: TestClient) -> None:
    payload = [{"id": 1, "label": "fulltime"}, {"id": 2, "label": "freelance"}]
    with respx.mock(assert_all_called=False) as r:
        r.get(f"{_UPSTREAM_BASE}/v_sources").respond(status_code=200, json=payload)
        resp = client.get("/api/postgrest/v_sources")
    assert resp.status_code == 200
    assert resp.json() == payload


def test_proxy_relays_404_from_unknown_view(client: TestClient) -> None:
    """PostgREST returns 404 for an unknown view name. The proxy must
    relay it untouched so the frontend's `getView` can surface the
    actual error to the user (rather than masking it as a generic 5xx)."""
    body = {"message": "no such relation: dash.v_unknown"}
    with respx.mock(assert_all_called=False) as r:
        r.get(f"{_UPSTREAM_BASE}/v_unknown").respond(status_code=404, json=body)
        resp = client.get("/api/postgrest/v_unknown")
    assert resp.status_code == 404
    assert resp.json() == body


def test_proxy_relays_401_role_missing(client: TestClient) -> None:
    """PostgREST returns 401 when the JWT role isn't granted SELECT on
    the target view. The proxy must relay it so the runbook's
    'role grants missing' branch is observable from the frontend."""
    body = {"message": "permission denied for view v_costs", "code": "42501"}
    with respx.mock(assert_all_called=False) as r:
        r.get(f"{_UPSTREAM_BASE}/v_costs").respond(status_code=401, json=body)
        resp = client.get("/api/postgrest/v_costs")
    assert resp.status_code == 401
    assert resp.json() == body


# ---------------------------------------------------------------------------
# 5. Upstream timeout / connect failure → gateway error, never hang.
# ---------------------------------------------------------------------------


@pytest.mark.smoke
def test_proxy_returns_gateway_error_when_upstream_unreachable(
    client: TestClient,
) -> None:
    """PostgREST container down ⇒ proxy returns a gateway-error status
    (502 / 503 / 504), NEVER hangs the caller.

    Failure mode covered: `docker compose ps` shows postgrest exited;
    the runbook's troubleshooting section ('stale dot') depends on
    this round-trip completing in finite time. A hung request would
    block the dashboard's auto-refresh loop indefinitely.
    """

    def boom(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise httpx.ConnectError("connection refused", request=request)

    with respx.mock(assert_all_called=False) as r:
        r.route(host="postgrest").mock(side_effect=boom)
        resp = client.get("/api/postgrest/v_overview")

    assert resp.status_code in (502, 503, 504), f"expected 502/503/504 on upstream failure, got {resp.status_code}"


def test_proxy_returns_gateway_error_when_upstream_times_out(
    client: TestClient,
) -> None:
    """Slow PostgREST ⇒ proxy enforces its own timeout and surfaces a
    gateway-error status. The proxy MUST set a finite timeout on its
    httpx client; without one a single slow query would block uvicorn
    workers for ranker-worker scoring loops too."""

    def boom(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise httpx.ReadTimeout("upstream too slow", request=request)

    with respx.mock(assert_all_called=False) as r:
        r.route(host="postgrest").mock(side_effect=boom)
        resp = client.get("/api/postgrest/v_overview")

    assert resp.status_code in (502, 503, 504), f"expected 502/503/504 on upstream timeout, got {resp.status_code}"
