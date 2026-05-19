"""Phase 5.2 — read-only reverse proxy to PostgREST.

Contract (pinned by `tests/api/test_postgrest_proxy.py` + documented in
`docs/runbooks/dashboard.md`):

  * Mount: APIRouter exporting `router`, intercepts `/api/postgrest/*`.
  * Allow-listed methods: GET, HEAD, OPTIONS. Everything else → 405,
    NEVER reaches PostgREST.
  * Forwards to `http://postgrest:3000/<path>?<querystring>` (Docker-internal
    DNS; no host port published; Tailscale-only ingress via api-service).
  * STRIPS `Authorization` and `Cookie` headers in BOTH directions so a
    same-origin script (or a future XSS) cannot smuggle a stolen JWT past
    the trusted server-set role.
  * Relays upstream status code + body verbatim (200, 401, 404, 5xx all
    pass through unchanged).
  * Finite timeout on the httpx client; unreachable / slow upstream
    surfaces as 502 / 504 — NEVER hangs.

Why a proxy at all (when PostgREST is "free" to expose directly):
  1. Same-origin requests — no CORS, no JWT, no API key handling in the
     browser. Same Tailscale ACL pin already protects api-service.
  2. The proxy is the FIRST line of defence on read-only. The PostgREST
     `pgrst_anon` role's GRANTs are the SECOND line. If V0XX accidentally
     widens grants, the proxy still refuses writes.
  3. Single ingress for the dashboard frontend: static assets + data on
     the same origin → trivial fetch('/api/postgrest/v_overview').
"""

from __future__ import annotations

import os

import httpx
from fastapi import APIRouter, HTTPException, Request, Response

from src.common.logger import get_logger

_log = get_logger(__name__)

# HTTP status codes the proxy emits. Named constants so a typo doesn't
# silently mis-classify a response (e.g. 405 vs 404).
_STATUS_METHOD_NOT_ALLOWED = 405
_STATUS_BAD_GATEWAY = 502
_STATUS_GATEWAY_TIMEOUT = 504

# Upstream connection budget. Connect is short — postgrest is one
# docker-DNS hop away — read is generous enough for the biggest dash view
# (currently ~30 rows) without pinning the uvicorn worker.
_UPSTREAM_CONNECT_TIMEOUT_S = 2.0
_UPSTREAM_READ_TIMEOUT_S = 10.0
_UPSTREAM_WRITE_TIMEOUT_S = 10.0
_UPSTREAM_POOL_TIMEOUT_S = 2.0

# Default PostgREST upstream URL inside the Docker `internal` network.
# Matches the compose service name + the port published by V019's
# postgrest service. Tests override via `_UPSTREAM_BASE` (respx mock).
_DEFAULT_POSTGREST_BASE_URL = "http://postgrest:3000"

# PostgREST service hostname inside the Docker `internal` network. The
# default matches the compose service name; tests override via the
# httpx-base-url-controlled `_UPSTREAM_BASE` (respx mock).
POSTGREST_BASE_URL = os.environ.get("POSTGREST_BASE_URL", _DEFAULT_POSTGREST_BASE_URL)

# Read-only HTTP methods the proxy will forward. Everything else gets 405.
# OPTIONS is included so PostgREST's auto-generated OpenAPI doc / CORS
# preflight remain reachable from the browser without a special case.
_ALLOWED_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})

# Headers we drop on the way OUT (browser → postgrest) AND on the way IN
# (postgrest → browser).
#
#   * `host` collides with PostgREST's virtual-host parsing.
#   * `content-length` is rewritten by httpx + Starlette respectively, so
#     forwarding it leads to body/length mismatches.
#   * hop-by-hop headers (RFC 7230 §6.1) must never be forwarded by a
#     proxy, by definition.
#   * `authorization` + `cookie` are stripped in BOTH directions to keep
#     the dashboard's same-origin model honest:
#       - Outbound: a client-set Authorization could escalate from
#         pgrst_anon to any role they name; a Cookie could be parsed by
#         a future PostgREST extension we did not audit.
#       - Inbound: PostgREST sometimes echoes credentials on errors; we
#         don't want those leaking into browser dev-tools logs.
_STRIP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
        "authorization",
        "cookie",
        "set-cookie",
    }
)


def _filter_headers(headers: dict[str, str] | httpx.Headers) -> dict[str, str]:
    """Drop hop-by-hop, host, content-length, and credential headers."""
    return {k: v for k, v in headers.items() if k.lower() not in _STRIP_HEADERS}


router = APIRouter()


# A single shared AsyncClient — connection pooling + keep-alives matter
# because PostgREST is the dashboard's hot path. The httpx client is
# created on first use (so test fixtures can monkeypatch the base_url) and
# closed via `aclose_proxy_client()` from the parent app's lifespan.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=POSTGREST_BASE_URL,
            # Bounded timeouts on every phase. Without these, a slow
            # PostgREST query would pin a uvicorn worker indefinitely;
            # that's the exact failure mode the proxy's gateway-error
            # contract is supposed to prevent.
            timeout=httpx.Timeout(
                connect=_UPSTREAM_CONNECT_TIMEOUT_S,
                read=_UPSTREAM_READ_TIMEOUT_S,
                write=_UPSTREAM_WRITE_TIMEOUT_S,
                pool=_UPSTREAM_POOL_TIMEOUT_S,
            ),
            # PostgREST is Docker-internal — no redirects, no retries.
            follow_redirects=False,
        )
    return _client


async def aclose_proxy_client() -> None:
    """Close the shared httpx client. Called from `src.api.main:lifespan`."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _assert_method_allowed(method: str, path: str) -> None:
    """Refuse writes with a structured 405. Pure guard — no I/O."""
    if method in _ALLOWED_METHODS:
        return
    _log.warning("postgrest_proxy_method_rejected", method=method, path=path)
    raise HTTPException(
        status_code=_STATUS_METHOD_NOT_ALLOWED,
        detail=f"method {method} not allowed; read-only proxy",
    )


def _build_target(path: str, query: str) -> str:
    """Compose the upstream URL path. Querystring forwarded verbatim so
    PostgREST's `?select=`, `?order=`, `?limit=` semantics pass through.
    """
    target = f"/{path}"
    if query:
        target = f"{target}?{query}"
    return target


async def _forward(method: str, path: str, target: str, headers: dict[str, str]) -> Response:
    """Issue the upstream request and map errors onto gateway responses."""
    client = _get_client()
    try:
        upstream = await client.request(method, target, headers=headers)
    except httpx.TimeoutException as exc:
        _log.error("postgrest_proxy_upstream_timeout", method=method, path=path, error=str(exc))
        return Response(
            content=b'{"error":"postgrest upstream timeout"}',
            status_code=_STATUS_GATEWAY_TIMEOUT,
            media_type="application/json",
        )
    except httpx.RequestError as exc:
        _log.error("postgrest_proxy_upstream_error", method=method, path=path, error=str(exc))
        return Response(
            content=b'{"error":"postgrest unreachable"}',
            status_code=_STATUS_BAD_GATEWAY,
            media_type="application/json",
        )
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=_filter_headers(upstream.headers),
        media_type=upstream.headers.get("content-type"),
    )


@router.api_route(
    "/api/postgrest/{path:path}",
    methods=["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"],
)
async def postgrest_proxy(path: str, request: Request) -> Response:
    """Forward GET / HEAD / OPTIONS to PostgREST. Refuse writes with 405.

    Every HTTP verb is registered on FastAPI's side so write methods hit
    THIS handler (returning a structured 405 + log line) instead of
    falling through to a framework-emitted 405 that bypasses the audit
    trail.
    """
    method = request.method.upper()
    _assert_method_allowed(method, path)
    target = _build_target(path, request.url.query)
    headers = _filter_headers(dict(request.headers))
    return await _forward(method, path, target, headers)
