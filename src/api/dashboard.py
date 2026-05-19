"""Phase 5.2 — static frontend bundle mount.

Pairs with `src/api/postgrest_proxy.py`:

  Browser → api-service (Tailscale-only)
              ├── /dashboard/        ← StaticFiles, this module
              └── /api/postgrest/*   ← reverse proxy, postgrest_proxy.py
                            ↓
                  postgrest:3000 (Docker-internal, dash schema, pgrst_anon)
                            ↓
                  postgres

Why this is a separate module:
  - `postgrest_proxy.py` is pinned by `tests/api/test_postgrest_proxy.py`
    as the canonical proxy module name. Mixing the StaticFiles mount in
    there would pollute the test target.
  - The mount is GUARDED: api-service still boots when the frontend agent
    has not yet committed `dashboard/index.html`. That guard belongs at
    composition time (`mount_dashboard(app)`), not inside a router.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from src.common.logger import get_logger

_log = get_logger(__name__)

# Path where compose bind-mounts ./dashboard. The api-service container is
# the only consumer; the mount is read-only on the container side.
DASHBOARD_DIR = Path("/app/dashboard")


def mount_dashboard(app: FastAPI) -> None:
    """Mount the static dashboard bundle at /dashboard/ if present.

    The mount is GUARDED: when `dashboard/index.html` is absent (e.g. the
    frontend agent hasn't committed yet, or the operator is running a
    backend-only smoke), the api-service still boots and the proxy stays
    functional. Frontend rollout is decoupled from backend rollout.
    """
    index_html = DASHBOARD_DIR / "index.html"
    if index_html.exists():
        # `html=True` makes StaticFiles serve /dashboard/ → index.html
        # automatically, which is what every SPA-ish frontend wants.
        app.mount(
            "/dashboard",
            StaticFiles(directory=str(DASHBOARD_DIR), html=True),
            name="dashboard",
        )
        _log.info("dashboard_mounted", path=str(DASHBOARD_DIR))
    else:
        _log.info(
            "dashboard_skipped_no_index",
            path=str(index_html),
            reason="frontend bundle not present; api boots without /dashboard/ mount",
        )
