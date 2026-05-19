"""Shared fixtures for the integration test lane.

Spins up an ephemeral ``pgvector/pgvector:pg16`` container (mirroring
``scripts/validate_migrations.sh``) on a per-session basis, replays
every ``migrations/V*.sql`` in V-number order, seeds the minimal rows
the dashboard views need, and yields a tenant-aware ``asyncpg``
connection + a dedicated Docker network the PostgREST fixture
hooks into.

Hard rules baked in (see ``CLAUDE.md``):
  * **No host port mapping.** The Postgres container exposes nothing
    on the host; the fixture talks to it via the container's
    docker-bridge IP (Linux-only — fine for the Pi 5 and CI Linux
    runners; macOS/Windows users hit the auto-skip below because
    ``docker.from_env()`` can't reach the daemon socket the same way).
  * **Tmpfs data dir.** Same rationale as
    ``scripts/validate_migrations.sh``: prod durability config
    (synchronous_commit=on, full_page_writes=on) is irrelevant when
    the container never touches disk.
  * **Auto-skip when Docker absent.** Every fixture funnels through
    ``_docker_client_or_skip()`` which catches missing-SDK, broken-stub
    namespace, and unreachable-daemon failures alike and re-emits a
    clean ``pytest.skip``. CI without Docker reports the suite as
    skipped, never errored.
  * **Teardown on failure.** The container is removed in a
    ``try/finally`` block so a mid-test crash never leaks state across
    sessions.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

# We import `docker` lazily inside `_docker_client_or_skip()` rather
# than at module-load time. Reasons:
#   1. The integration tests are deselected from the default lane via
#      `-m "not integration"` (see pyproject.toml). The module still
#      gets imported during collection though, so any import-time skip
#      here is wasteful when the marker filter would have skipped it
#      anyway.
#   2. `importorskip` at conftest top-level is fragile: a stub
#      `docker` namespace package (one without `from_env`) makes
#      importorskip succeed but later attribute access fail with
#      `AttributeError`, NOT a Skipped exception. Doing the import
#      inside the fixture lets us catch BOTH ModuleNotFoundError
#      AND missing-attribute errors and re-emit a clean pytest.skip.
#   3. Skip-at-module-level inside conftest.py crashes pytest
#      collection on some versions because the conftest is treated
#      as required infrastructure. Skip inside a fixture is safe.

# ---------------------------------------------------------------------------
# Container image + connection constants.
# ---------------------------------------------------------------------------

# Multi-arch image — matches scripts/validate_migrations.sh and prod
# (compose.yaml `postgres` service). amd64 + arm64.
PG_IMAGE = "pgvector/pgvector:pg16"

# PostgREST upstream image — matches compose.yaml `postgrest` service.
POSTGREST_IMAGE = "postgrest/postgrest:v12.0.2"

# Test-scoped credentials. NOT secrets — the container is ephemeral,
# tmpfs-only, and unreachable outside this Docker network.
PG_USER = "postgres"
PG_PASSWORD = "integration_test_pw"  # ephemeral test container password
PG_DB = "cartograph_integration"
PG_PORT_INTERNAL = 5432
POSTGREST_PORT_INTERNAL = 3000

# Path to the migrations dir, relative to repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATIONS_DIR = _REPO_ROOT / "migrations"


# ---------------------------------------------------------------------------
# Daemon probe — collect-time skip when the daemon is unreachable.
# ---------------------------------------------------------------------------


def _docker_client_or_skip():  # type: ignore[no-untyped-def]
    """Return a docker client or pytest.skip the test.

    Failure modes covered (every one re-emits as a clean skip, never
    a noisy traceback through the test runner):
      * ``docker`` SDK not installed at all.
      * ``docker`` is a stub namespace package without ``from_env``.
      * Daemon socket is missing / permission-denied / down.
    """
    try:
        import docker as docker_sdk  # local import — lazy / skip-safe
    except ImportError as exc:
        pytest.skip(f"docker SDK not installed: {exc}")
    if not hasattr(docker_sdk, "from_env"):
        pytest.skip(
            f"docker module at {getattr(docker_sdk, '__file__', '?')} lacks "
            "`from_env` — install the real `docker` SDK (`uv pip install docker`)."
        )
    try:
        client = docker_sdk.from_env()
        client.ping()
    except Exception as exc:  # pragma: no cover — environment-dependent
        pytest.skip(f"docker daemon unreachable: {exc}")
    return client


# ---------------------------------------------------------------------------
# Helpers for waiting on TCP + Postgres readiness from the host.
# ---------------------------------------------------------------------------


def _wait_tcp(host: str, port: int, timeout_s: float = 30.0) -> None:
    """Poll a TCP port until it accepts a connection or times out."""
    deadline = time.monotonic() + timeout_s
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError as exc:
            last_err = exc
            time.sleep(0.25)
    raise TimeoutError(f"TCP {host}:{port} never opened: {last_err}")


async def _wait_pg_ready(dsn: str, timeout_s: float = 30.0) -> None:
    """Poll asyncpg until SELECT 1 succeeds."""
    deadline = time.monotonic() + timeout_s
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            conn = await asyncpg.connect(dsn=dsn, timeout=2.0)
            try:
                await conn.execute("SELECT 1")
                return
            finally:
                await conn.close()
        except Exception as exc:  # asyncpg + socket errors
            last_err = exc
            await asyncio.sleep(0.25)
    raise TimeoutError(f"postgres never accepted SELECT 1: {last_err}")


# ---------------------------------------------------------------------------
# Fixture: ephemeral Docker network — both pg + postgrest join it.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def docker_client():  # type: ignore[no-untyped-def]
    return _docker_client_or_skip()


@pytest.fixture(scope="session")
def docker_network(docker_client):  # type: ignore[no-untyped-def]
    """Bridge network shared by the pg + postgrest containers."""
    name = f"cartograph-integration-net-{os.getpid()}"
    network = docker_client.networks.create(name, driver="bridge")
    try:
        yield network
    finally:
        try:
            network.remove()
        except Exception:  # pragma: no cover — cleanup best-effort
            pass


# ---------------------------------------------------------------------------
# Fixture: ephemeral Postgres container + migrations replay + seeds.
# ---------------------------------------------------------------------------


def _read_migration_files() -> list[Path]:
    """V-number-sorted migration files (same as validate_migrations.sh)."""
    files = sorted(_MIGRATIONS_DIR.glob("V*.sql"))
    if not files:  # pragma: no cover — repo invariant
        raise RuntimeError(f"no V*.sql files under {_MIGRATIONS_DIR}")
    return files


def _container_ip(container, network_name: str) -> str:
    """Resolve the container's IP on the named docker network."""
    container.reload()
    nets = container.attrs["NetworkSettings"]["Networks"]
    if network_name not in nets:  # pragma: no cover — defensive
        raise RuntimeError(f"container not attached to {network_name}: {list(nets)}")
    ip = nets[network_name]["IPAddress"]
    if not ip:  # pragma: no cover — defensive
        raise RuntimeError(f"container has no IP on {network_name}")
    return ip


@pytest_asyncio.fixture(scope="session")
async def pg_container(docker_client, docker_network):  # type: ignore[no-untyped-def]
    """Boot pgvector container, replay every V*.sql, return (container, dsn).

    The container has NO host port published — we talk to it through
    its docker-bridge IP. The host-routable IP works on Linux because
    docker0 is just a bridge interface on the host; this is the same
    path ``scripts/validate_migrations.sh`` would take if it weren't
    using ``docker exec`` for psql.
    """
    container_name = f"cartograph-integration-pg-{os.getpid()}"
    container = docker_client.containers.run(
        PG_IMAGE,
        name=container_name,
        environment={
            "POSTGRES_USER": PG_USER,
            "POSTGRES_PASSWORD": PG_PASSWORD,
            "POSTGRES_DB": PG_DB,
        },
        # tmpfs mount = fast + ephemeral. Matches validate_migrations.sh.
        tmpfs={"/var/lib/postgresql/data": ""},
        network=docker_network.name,
        detach=True,
        remove=False,  # we remove manually in finally
        # Do NOT publish ports. CLAUDE.md hard rule.
    )

    try:
        ip = _container_ip(container, docker_network.name)
        dsn = f"postgresql://{PG_USER}:{PG_PASSWORD}@{ip}:{PG_PORT_INTERNAL}/{PG_DB}"

        _wait_tcp(ip, PG_PORT_INTERNAL, timeout_s=30.0)
        await _wait_pg_ready(dsn, timeout_s=30.0)

        # Replay migrations in V-number order. Each migration file is
        # already wrapped in BEGIN/COMMIT (per CLAUDE.md "no-wipe retry"
        # contract) so asyncpg.execute on the file body Just Works.
        conn = await asyncpg.connect(dsn=dsn)
        try:
            for migration_path in _read_migration_files():
                sql = migration_path.read_text()
                try:
                    await conn.execute(sql)
                except Exception as exc:  # pragma: no cover — debug surface
                    raise RuntimeError(f"migration {migration_path.name} failed: {exc}") from exc

            # Minimal seed: user row already exists from V001 seed. We
            # need at least one source + one opportunity + one
            # application + one usage_ledger row so the views return
            # non-empty rows where the dashboard expects them, AND
            # at least one ranker_weights_fit + one source_refit_log
            # row so v_ranker_fits / v_source_refits are non-empty.
            await _seed_minimal(conn)
        finally:
            await conn.close()

        yield container, dsn
    finally:
        try:
            container.remove(force=True)
        except Exception:  # pragma: no cover — cleanup best-effort
            pass


async def _seed_minimal(conn: asyncpg.Connection) -> None:
    """Seed the smallest data shape the 7 dashboard views need.

    Notes:
      * V001 already inserts ``users(id=1)``. We rely on that here.
      * V003 already seeds the ``sources`` table from
        ``config/sources/*.yaml``. We rely on that for v_source_health
        without inserting more, but we still need at least one
        opportunity tied to one of those sources so v_recent_opps
        returns a row.
      * V017 dropped ``user_id DEFAULT 1`` on every per-user table, so
        every INSERT below carries the tenant id explicitly.
    """
    # Pick any seeded source (V003 inserts dozens; we just need one).
    src_row = await conn.fetchrow("SELECT id, slug FROM sources LIMIT 1")
    if src_row is None:
        # Defensive — if V003 ever stops seeding, insert a minimal source.
        # The CHECK constraint on sources.category limits the value space;
        # 'other' is a safe choice.
        src_id = await conn.fetchval(
            """
            INSERT INTO sources (slug, name, category, base_url, crawler_strategy)
            VALUES ('integration-test', 'Integration Test',
                    'other', 'https://example.invalid/', 'noop')
            RETURNING id
            """,
        )
    else:
        src_id = src_row["id"]

    # One opportunity. The HNSW vector index doesn't reject NULL embeddings;
    # we leave the embedding NULL to avoid carrying the pgvector adapter.
    opp_id = await conn.fetchval(
        """
        INSERT INTO opportunities (
            source_id, canonical_url, title, company, category,
            fingerprint_hash, state
        )
        VALUES ($1, $2, 'Test Engineer', 'Acme Co', 'fulltime',
                'integration-test-fp', 'new')
        RETURNING id
        """,
        src_id,
        f"https://example.invalid/opp/{os.getpid()}",
    )

    # One opportunity_scores row so v_recent_opps gets a non-NULL score.
    await conn.execute(
        """
        INSERT INTO opportunity_scores (user_id, opportunity_id, score, score_components)
        VALUES (1, $1, 0.75,
                '{"kw_match": 0.4, "embedding_sim": 0.3, "comp_score": 0.05}'::jsonb)
        ON CONFLICT (user_id, opportunity_id) DO NOTHING
        """,
        opp_id,
    )

    # One application (so v_recent_applications + v_overview counters move).
    # apply_method enum: email|ats_form|external|in_platform|embedded_form.
    await conn.execute(
        """
        INSERT INTO applications (user_id, opportunity_id, method, response_status)
        VALUES (1, $1, 'email', 'pending')
        ON CONFLICT (user_id, opportunity_id) DO NOTHING
        """,
        opp_id,
    )

    # One usage_ledger row inside the 30-day window so v_cost_daily is
    # non-empty. usage_kind_enum is defined in V001; 'llm_extract' is a
    # safe value (extractor cost — see src/common/llm.py). Matches the
    # comment immediately above — the literal 'llm' here was a typo that
    # the enum check (V001 line 341-342) rejected, blocking the entire
    # integration suite.
    await conn.execute(
        """
        INSERT INTO usage_ledger
            (user_id, kind, provider, model, input_tokens, output_tokens, cost_usd_micros)
        VALUES (1, 'llm_extract', 'openrouter', 'gemini-flash', 100, 50, 25000)
        """,
    )

    # One ranker_weights_fit row so v_ranker_fits is non-empty.
    await conn.execute(
        """
        INSERT INTO ranker_weights_fit (
            user_id, rows_used, positive_rate, auc, status,
            kw_match, embedding_sim, comp_score,
            freshness, source_quality, response_rate
        )
        VALUES (1, 100, 0.05, 0.72, 'ok',
                0.30, 0.25, 0.15,
                0.10, 0.10, 0.10)
        """,
    )

    # One source_refit_log row so v_source_refits is non-empty.
    await conn.execute(
        """
        INSERT INTO source_refit_log
            (rows_used, positive_rate, auc, status, weight_writes)
        VALUES (50, 0.04, 0.65, 'ok', 7)
        """,
    )


@pytest_asyncio.fixture(scope="function")
async def pg_conn(pg_container) -> AsyncIterator[asyncpg.Connection]:
    """A fresh asyncpg connection per test, closed on teardown."""
    _container, dsn = pg_container
    conn = await asyncpg.connect(dsn=dsn)
    try:
        yield conn
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Fixture: ephemeral PostgREST container + the FastAPI proxy.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def postgrest_container(docker_client, docker_network, pg_container) -> Iterator[tuple]:
    """Boot PostgREST against the pg container, return (container, base_url).

    PostgREST is wired through Docker DNS to ``pg_container`` (it joins
    the same ``docker_network``), so its libpq connection string can use
    the container name. The FastAPI proxy in turn talks to PostgREST
    through that same network via its docker-bridge IP — we DON'T
    publish a host port here either.
    """
    pg_cont, _dsn = pg_container

    container_name = f"cartograph-integration-postgrest-{os.getpid()}"
    container = docker_client.containers.run(
        POSTGREST_IMAGE,
        name=container_name,
        environment={
            # Keyword/value DSN form (libpq) — same as compose.yaml's
            # PGRST_DB_URI, so passwords with `/`, `+`, `=` parse safely.
            "PGRST_DB_URI": (f"host={pg_cont.name} port={PG_PORT_INTERNAL} user={PG_USER} password={PG_PASSWORD} dbname={PG_DB}"),
            "PGRST_DB_SCHEMAS": "dash",
            "PGRST_DB_ANON_ROLE": "pgrst_anon",
            "PGRST_SERVER_PORT": str(POSTGREST_PORT_INTERNAL),
            "PGRST_JWT_SECRET": "",
        },
        network=docker_network.name,
        detach=True,
        remove=False,
    )

    try:
        ip = _container_ip(container, docker_network.name)
        base_url = f"http://{ip}:{POSTGREST_PORT_INTERNAL}"

        # Wait for PostgREST to start accepting HTTP. It needs to
        # connect to Postgres + reload its schema cache before the
        # first request lands; ~2-5 s on a warm machine.
        _wait_tcp(ip, POSTGREST_PORT_INTERNAL, timeout_s=30.0)

        yield container, base_url
    finally:
        try:
            container.remove(force=True)
        except Exception:  # pragma: no cover — cleanup best-effort
            pass


@pytest_asyncio.fixture(scope="function")
async def proxy_client(postgrest_container, monkeypatch):  # type: ignore[no-untyped-def]
    """Mount the real FastAPI proxy in front of the real PostgREST.

    We point the proxy's httpx client at the PostgREST docker-bridge
    URL by patching ``POSTGREST_BASE_URL`` BEFORE the proxy module's
    lazy ``_get_client()`` builds its httpx.AsyncClient. The proxy
    module caches the client in a module-level ``_client``; we close
    + reset it on teardown so the next test re-builds with the right
    upstream.
    """
    import httpx  # local import keeps top-level deps small
    from fastapi import FastAPI

    _container, base_url = postgrest_container

    # Patch the upstream URL the proxy reads at client-build time.
    # The proxy reads ``POSTGREST_BASE_URL`` at module-load time, so we
    # set the env var THEN reload the module so the constant picks up
    # the new value. Simpler than monkeypatching the global.
    import importlib

    monkeypatch.setenv("POSTGREST_BASE_URL", base_url)
    proxy_mod = importlib.import_module("src.api.postgrest_proxy")
    proxy_mod = importlib.reload(proxy_mod)

    # Belt-and-braces: also write the module-level constant directly,
    # so the test still works if importlib.reload is somehow no-op
    # under a future cached-bytecode scheme.
    proxy_mod.POSTGREST_BASE_URL = base_url
    # Force a fresh httpx client bound to the new base_url.
    await proxy_mod.aclose_proxy_client()

    app = FastAPI()
    app.include_router(proxy_mod.router)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        try:
            yield client
        finally:
            await proxy_mod.aclose_proxy_client()
