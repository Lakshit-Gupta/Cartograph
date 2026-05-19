"""Integration test lane — real Postgres + PostgREST + FastAPI proxy.

Opt-in. The default ``pytest`` run deselects ``-m integration`` (see
``pyproject.toml -> tool.pytest.ini_options.addopts``). Run explicitly:

    uv run pytest -m integration tests/integration/ -q --no-header

Every test in this package boots an ephemeral
``pgvector/pgvector:pg16`` container with tmpfs data dir (mirrors
``scripts/validate_migrations.sh``) — NO host port mapping per
``CLAUDE.md`` (Docker-internal network only). Tests SKIP automatically
when the Python ``docker`` SDK is not installed (``importorskip``) or
when the Docker daemon is unreachable.
"""
