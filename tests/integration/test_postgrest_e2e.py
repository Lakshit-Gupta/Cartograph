"""End-to-end smoke through the real Postgres + real PostgREST + FastAPI proxy.

Unlike ``tests/api/test_postgrest_proxy.py`` (hermetic, respx-mocked),
this lane exercises the production wiring:

  browser → FastAPI proxy (`src/api/postgrest_proxy.py`)
          → real PostgREST container (over docker-bridge IP)
          → real Postgres container (V001..V019 replayed, seeded)
          → ``dash.*`` view
          → JSON array back to the browser

If any of those layers reshape the payload (column rename in V0XX,
PostgREST schema-cache stale, proxy header-stripping regression), the
tests here fail in CI. The unit-test lane stays cheap; this lane
catches things mocks cannot.
"""

from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# 1. GET /api/postgrest/<view> returns the live JSON array.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "view",
    [
        "v_overview",
        "v_recent_opps",
        "v_recent_applications",
        "v_cost_daily",
        "v_source_health",
        "v_ranker_fits",
        "v_source_refits",
    ],
)
async def test_view_round_trip_returns_json_array(
    proxy_client: httpx.AsyncClient,
    view: str,
) -> None:
    """Every dashboard view must return a JSON list to the browser.

    The proxy relays the upstream body verbatim; PostgREST always
    returns ``application/json`` arrays for ``GET /<view>``. If the
    body is anything else (HTML 500 page, plain text error, etc.) the
    frontend's ``getView`` throws — which is the failure we want to
    catch here at the platform layer, not at the user's browser.
    """
    resp = await proxy_client.get(f"/api/postgrest/{view}")
    assert resp.status_code == 200, (resp.status_code, resp.text[:500])
    assert resp.headers["content-type"].startswith("application/json"), resp.headers
    payload = resp.json()
    assert isinstance(payload, list), f"expected JSON array, got {type(payload).__name__}"


async def test_v_overview_returns_single_row_via_proxy(
    proxy_client: httpx.AsyncClient,
) -> None:
    """``v_overview`` is a single-row aggregate. End-to-end the proxy
    should hand the browser a list of length 1 — the dashboard's
    ``getOne()`` helper destructures ``rows[0]``."""
    resp = await proxy_client.get("/api/postgrest/v_overview")
    assert resp.status_code == 200, resp.text[:500]
    rows = resp.json()
    assert isinstance(rows, list)
    assert len(rows) == 1, f"v_overview must return 1 row, got {len(rows)}"


async def test_v_recent_opps_round_trip_includes_seed(
    proxy_client: httpx.AsyncClient,
) -> None:
    """The seed in conftest plants one opportunity. Verify it lands in
    the view payload through the full proxy → postgrest → view chain."""
    resp = await proxy_client.get("/api/postgrest/v_recent_opps")
    assert resp.status_code == 200, resp.text[:500]
    rows = resp.json()
    assert isinstance(rows, list)
    assert len(rows) >= 1, "expected at least the seeded opportunity"
    # Every row must carry the keys the frontend reads.
    first = rows[0]
    for key in ("title", "company", "category", "score", "score_components"):
        assert key in first, f"v_recent_opps row missing key {key}: {sorted(first.keys())}"


# ---------------------------------------------------------------------------
# 2. PostgREST query semantics survive the proxy.
# ---------------------------------------------------------------------------


async def test_postgrest_select_filter_is_forwarded(
    proxy_client: httpx.AsyncClient,
) -> None:
    """PostgREST's ``?select=col1,col2`` projection must reach upstream
    via the proxy's querystring-verbatim forwarding."""
    resp = await proxy_client.get(
        "/api/postgrest/v_overview",
        params={"select": "opps_24h,applied_today"},
    )
    assert resp.status_code == 200, resp.text[:500]
    rows = resp.json()
    assert isinstance(rows, list) and rows, rows
    keys = set(rows[0].keys())
    # The PostgREST ?select= projects ONLY the requested columns; we
    # assert tight equality so a regression that drops or fails to
    # forward the param surfaces immediately.
    assert keys == {"opps_24h", "applied_today"}, keys


async def test_postgrest_limit_param_is_forwarded(
    proxy_client: httpx.AsyncClient,
) -> None:
    """``?limit=N`` must cap the row count end-to-end."""
    resp = await proxy_client.get(
        "/api/postgrest/v_source_health",
        params={"limit": "3"},
    )
    assert resp.status_code == 200, resp.text[:500]
    rows = resp.json()
    assert isinstance(rows, list)
    assert len(rows) <= 3, len(rows)


# ---------------------------------------------------------------------------
# 3. Write methods get 405 from the proxy — NEVER reach PostgREST.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_write_methods_rejected_with_405(
    proxy_client: httpx.AsyncClient,
    method: str,
) -> None:
    """Read-only contract under load — even the real PostgREST is
    rejected by the proxy when a write verb is sent. This is the
    primary defence against same-origin XSS escalation; the role
    GRANT in V019 is the second line."""
    resp = await proxy_client.request(
        method,
        "/api/postgrest/v_overview",
        json={"opps_24h": 9999},
    )
    assert resp.status_code == 405, (method, resp.status_code, resp.text[:200])


# ---------------------------------------------------------------------------
# 4. Unknown view name surfaces as 404 — the proxy relays it verbatim.
# ---------------------------------------------------------------------------


async def test_unknown_view_returns_404_via_proxy(
    proxy_client: httpx.AsyncClient,
) -> None:
    """PostgREST returns 404 (``"PGRST"`` family error) for an unknown
    relation. The proxy MUST relay it so the dashboard's ``getView``
    surfaces the actual error rather than masking it as 5xx."""
    resp = await proxy_client.get("/api/postgrest/v_no_such_view_zzz")
    assert resp.status_code == 404, (resp.status_code, resp.text[:200])
