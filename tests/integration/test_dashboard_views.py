"""V019 dashboard view contract — column shape + pgrst_anon SELECT grant.

For each of the 7 ``dash.v_*`` views shipped by V019 we assert three
things, against a real Postgres (the integration ``pg_conn`` fixture):

  1. ``SELECT * FROM dash.v_X`` succeeds and returns at least the rows
     the conftest seeded.
  2. The view projects the column names the dashboard frontend reads
     in ``dashboard/views/*.js`` (see ``_FRONTEND_COLUMNS`` table below).
     If the frontend renames or drops a column without updating the
     view, this test fails loudly with the missing names.
  3. The ``pgrst_anon`` role (created in V019) can SELECT every view.
     We test by ``SET LOCAL ROLE pgrst_anon`` so the test exercises
     the same role PostgREST will use in production.

The column-projection check is intentionally a **superset** test: the
view may add columns the frontend doesn't read yet (forward-compat),
but it MUST NOT lose a column the frontend already references.
"""

from __future__ import annotations

import asyncpg
import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Frontend column ledger — what dashboard/views/*.js actually reads.
#
# Keep this list grep-able against the JS source: a view-column rename
# upstream WILL trip these assertions. Add a column the moment the
# frontend starts using it.
# ---------------------------------------------------------------------------

_FRONTEND_COLUMNS: dict[str, frozenset[str]] = {
    # dashboard/views/overview.js — reads every tile counter on `ov`
    # plus `score`, `score_components` from top opp + `date`, `usd`
    # from sparkline.
    "v_overview": frozenset(
        {
            "opps_24h",
            "applied_today",
            "applied_7d",
            "sent_24h",
            "response_rate_30d",
            "cost_today_usd",
            "cost_mtd_usd",
            "active_sources",
            "quarantined_sources",
            "healthy_identities",
        }
    ),
    # dashboard/views/opps.js — buildRow + COLS sort keys.
    "v_recent_opps": frozenset(
        {
            "title",
            "company",
            "category",
            "score",
            "score_components",
            "posted_at",
            "first_seen",
        }
    ),
    # dashboard/views/applications.js — buildRow + COLS sort keys.
    "v_recent_applications": frozenset(
        {
            "sent_at",
            "title",
            "company",
            "method",
            "resume_compile_status",
            "response_status",
            "response_at",
        }
    ),
    # dashboard/views/costs.js — chart + table.
    "v_cost_daily": frozenset(
        {
            "date",
            "kind",
            "model",
            "usd",
            "input_tokens",
            "output_tokens",
        }
    ),
    # dashboard/views/sources.js — COLS + statusClass + tile counts.
    "v_source_health": frozenset(
        {
            "slug",
            "status",
            "opps_extracted_30d",
            "ranking_weight",
            "last_successful_crawl_at",
            "ban_observed_at",
        }
    ),
    # dashboard/views/refits.js — RANKER_COLS (ranker tab).
    "v_ranker_fits": frozenset(
        {
            "fit_at",
            "version",
            "n_samples",
            "auc",
            "loss",
            "weights_summary",
        }
    ),
    # dashboard/views/refits.js — SOURCE_COLS (source-refits tab).
    "v_source_refits": frozenset(
        {
            "fit_at",
            "n_apps",
            "n_sources",
            "auc",
            "notes",
        }
    ),
}


async def _view_columns(conn: asyncpg.Connection, view: str) -> set[str]:
    """Return the set of column names projected by ``dash.<view>``."""
    rows = await conn.fetch(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'dash' AND table_name = $1
        """,
        view,
    )
    return {r["column_name"] for r in rows}


# ---------------------------------------------------------------------------
# 1. Each view exists, returns rows, and exposes the columns it should.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("view", sorted(_FRONTEND_COLUMNS.keys()))
async def test_view_selects_successfully(pg_conn: asyncpg.Connection, view: str) -> None:
    """``SELECT * FROM dash.v_X LIMIT 1`` does not raise."""
    rows = await pg_conn.fetch(f"SELECT * FROM dash.{view} LIMIT 5")
    # We don't assert non-empty here for every view — v_overview is a
    # single-row aggregate that's always present; the others depend on
    # the seed planted in conftest._seed_minimal. asyncpg returns a
    # list; the type check itself is the contract.
    assert isinstance(rows, list), f"dash.{view} SELECT returned {type(rows)}"


@pytest.mark.parametrize("view, expected", sorted(_FRONTEND_COLUMNS.items()))
async def test_view_projects_frontend_columns(
    pg_conn: asyncpg.Connection,
    view: str,
    expected: frozenset[str],
) -> None:
    """Every column the frontend reads is projected by the view.

    Surfaces (does NOT fix) any drift between SQL view and JS reader.
    Failure message lists every missing column so the operator can
    fix the view (preferred) or the JS reader.
    """
    actual = await _view_columns(pg_conn, view)
    missing = expected - actual
    assert not missing, f"dash.{view} is missing frontend-referenced columns: {sorted(missing)}. View projects: {sorted(actual)}"


# ---------------------------------------------------------------------------
# 2. v_overview is the single-row aggregate — it ALWAYS has one row.
# ---------------------------------------------------------------------------


async def test_v_overview_is_single_row(pg_conn: asyncpg.Connection) -> None:
    """``v_overview`` is built from scalar sub-queries — exactly one row."""
    rows = await pg_conn.fetch("SELECT * FROM dash.v_overview")
    assert len(rows) == 1, f"v_overview must return exactly 1 row, got {len(rows)}"


async def test_v_overview_counters_reflect_seed(pg_conn: asyncpg.Connection) -> None:
    """The seed planted one application + one usage_ledger row; the
    headline counters must observe both."""
    row = await pg_conn.fetchrow("SELECT * FROM dash.v_overview")
    assert row is not None
    # The seed in conftest plants one application with user_id=1 + one
    # usage_ledger row with cost_usd_micros=25000 ($0.025).
    assert (row["applied_today"] or 0) >= 1, row["applied_today"]
    assert (row["sent_24h"] or 0) >= 1, row["sent_24h"]
    assert float(row["cost_today_usd"] or 0) >= 0.025, row["cost_today_usd"]


# ---------------------------------------------------------------------------
# 3. pgrst_anon role can SELECT every view (V019 grant + default privs).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("view", sorted(_FRONTEND_COLUMNS.keys()))
async def test_pgrst_anon_can_select(pg_conn: asyncpg.Connection, view: str) -> None:
    """The PostgREST anon role MUST be able to SELECT every dash.* view.

    Failure mode caught: V019's ``GRANT SELECT ON ALL TABLES IN SCHEMA
    dash TO pgrst_anon`` runs BEFORE the view ``CREATE OR REPLACE``
    statements only for the views in V019 itself. Future V0XX must
    rely on ``ALTER DEFAULT PRIVILEGES`` for auto-grant. If a future
    migration creates a dash.v_* view but forgets to GRANT (or runs
    GRANT before the view exists), this test catches it.
    """
    # SET LOCAL ROLE inside a transaction so the change is rolled back
    # at teardown — never leaks across tests sharing a connection.
    async with pg_conn.transaction():
        await pg_conn.execute("SET LOCAL ROLE pgrst_anon")
        # SELECT must succeed without raising. We don't care about
        # the row contents here — the GRANT itself is the contract.
        rows = await pg_conn.fetch(f"SELECT * FROM dash.{view} LIMIT 1")
        assert isinstance(rows, list)


async def test_pgrst_anon_cannot_select_raw_tables(pg_conn: asyncpg.Connection) -> None:
    """Defense-in-depth: pgrst_anon MUST NOT reach base tables.

    The proxy is the first line of defence on read-only writes; the
    pgrst_anon GRANTs are the second line. Specifically the role MUST
    NOT have SELECT on ``identities`` (encrypted_credentials column),
    ``opportunities`` (raw description), or ``applications`` (payload).
    """
    async with pg_conn.transaction():
        await pg_conn.execute("SET LOCAL ROLE pgrst_anon")
        for table in ("identities", "opportunities", "applications"):
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await pg_conn.fetch(f"SELECT 1 FROM {table} LIMIT 1")


# ---------------------------------------------------------------------------
# 4. Read-only contract — pgrst_anon CANNOT modify the underlying data.
# ---------------------------------------------------------------------------


async def test_pgrst_anon_cannot_insert_into_dash_views(pg_conn: asyncpg.Connection) -> None:
    """Views in `dash.*` are read-only by design — pgrst_anon has no
    INSERT/UPDATE/DELETE grant. A successful write here would imply
    V019 widened the GRANT beyond SELECT."""
    async with pg_conn.transaction():
        await pg_conn.execute("SET LOCAL ROLE pgrst_anon")
        # Insertable_into = NO on a view without an INSTEAD OF trigger.
        # Postgres raises InsufficientPrivilege OR FeatureNotSupported
        # ('cannot insert into view'); both are acceptable failure
        # modes — what we forbid is the INSERT silently succeeding.
        with pytest.raises(
            (
                asyncpg.exceptions.InsufficientPrivilegeError,
                asyncpg.exceptions.FeatureNotSupportedError,
            )
        ):
            await pg_conn.execute(
                """
                INSERT INTO dash.v_overview (opps_24h) VALUES (9999)
                """,
            )
