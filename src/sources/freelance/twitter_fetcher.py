"""Freelance Twitter/X founder-signal fetcher (Phase 3.1) — public module.

Polls a configurable list of Nitter mirrors (public Twitter front-ends that
don't require auth) for a user-curated set of founder/recruiter handles in
`config/profile/prefs.yaml -> freelance.twitter_handles`. Filters each handle's
recent tweets for hiring-intent keywords, parses matches into
`Opportunity` payloads, and publishes them directly onto `stream:rank` via
`persist_and_publish` — bypassing the crawler / extractor tiers (Nitter
output is structured enough that selectolax + regex is sufficient).

Hard constraints (see task brief + CLAUDE.md):
  * No Twitter API key required — Nitter only. All mirrors down ⇒ log +
    retry; never crash the loop.
  * Worker boots cleanly with `freelance.twitter_handles: []` — logs
    `tw_no_handles_configured` and idles.
  * Read-only: never reply / DM / follow / like. Just GET <mirror>/<handle>.
  * Rate-limit: 1 request per Nitter instance per 30s (per-instance lock).
  * Daily-fetch budget: 10 polls per handle per day.
  * Fingerprint = sha256(f"twitter:{handle}:{tweet_id}") — restart-safe.
  * Import-clean even when the network is down (httpx is lazy at run-time).

This module is the public surface tests / workers import as
`twitter_fetcher`. The heavy lifting (parser, mirror rotator, daily-cap
tracker, fetch loop) lives in the `twitter/` subpackage; this file hosts
the worker entrypoint + dedupe publisher so `monkeypatch.setattr(tw, ...)`
in `tests/sources/test_twitter_fetcher.py` keeps reaching the names that
`run()` / `_publish_with_dedupe` actually look up.
"""

from __future__ import annotations

import asyncio

import httpx

from src.common.db import close_pool, init_pool
from src.common.logger import get_logger
from src.common.queue import RedisQ
from src.common.types import Opportunity
from src.extractors.persist import persist_and_publish

# Re-exports — module-level rebinds so `monkeypatch.setattr(tw, name, ...)`
# patches the same binding that `run()` / `_publish_with_dedupe` resolve.
# Names below are imported solely to surface them on this module's namespace
# (tests + back-compat); ruff's F401 fires because we don't *call* them here.
from src.sources.freelance.twitter.cap import (
    _PER_HANDLE_DAILY_MAX,  # noqa: F401 — re-export for tests
    _DailyBudget,
)
from src.sources.freelance.twitter.mirrors import (
    _PER_MIRROR_MIN_GAP_SECONDS,
    NITTER_INSTANCES,
    _MirrorRotator,
)
from src.sources.freelance.twitter.parser import (
    TweetMatch,
    _fingerprint,  # noqa: F401 — re-export for tests
    _normalise_handle,  # noqa: F401 — re-export for tests
    _parse_nitter_timestamp,  # noqa: F401 — re-export for back-compat
    infer_category,
    matches_hiring,
    parse_tweet_html,
    tweet_to_opportunity,
)
from src.sources.freelance.twitter.poller import (
    PollContext,
    fetch_handle,
    load_handles_from_prefs,
    resolve_source_id,
)

_log = get_logger(__name__)

# ---- loop-level constants --------------------------------------------------

# Loop cadence. 30 min poll matches `sources.fetch_freq_minutes` row in V015.
_POLL_INTERVAL_SECONDS = 30 * 60

# HTTP request settings.
_HTTP_TIMEOUT = 12.0
_HTTP_USER_AGENT = "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Cartograph-Hop/1.0 (+contact via repo owner)"

# Idle loop tick when no handles are configured.
_IDLE_SLEEP_SECONDS = 300


# ---- publish path (kept in this module so persist_and_publish is patchable) -


async def _publish_with_dedupe(
    q: RedisQ,
    opp: Opportunity,
    *,
    handle: str,
    tweet_id: str,
) -> None:
    """Persist + publish. Swallow unique-violation (dedupe) at debug level."""
    try:
        opp_id = await persist_and_publish(q, opp)
        if opp_id is None:
            _log.debug("tw_dedupe_skip", handle=handle, tweet_id=tweet_id)
            return
        _log.info(
            "tw_opportunity_published",
            handle=handle,
            tweet_id=tweet_id,
            opportunity_id=str(opp_id),
        )
    except Exception as e:
        sqlstate = getattr(e, "sqlstate", None)
        if sqlstate == "23505":
            _log.debug(
                "tw_dedupe_skip",
                handle=handle,
                tweet_id=tweet_id,
                sqlstate=sqlstate,
            )
            return
        _log.exception("tw_publish_failed", handle=handle, tweet_id=tweet_id, err=str(e))


async def _poll_once(handles: list[str], ctx: PollContext) -> None:
    """One pass over every configured handle. Respects daily budget + per-mirror gap."""
    for handle in handles:
        if not ctx.budget.allowed(handle):
            _log.debug("tw_handle_budget_exhausted", handle=handle)
            continue
        # Wait for a mirror to be ready (per-mirror cool-down).
        wait = ctx.rotator.wait_hint()
        if wait > 0:
            await asyncio.sleep(min(wait, _PER_MIRROR_MIN_GAP_SECONDS))
        ctx.budget.increment(handle)
        matches = await fetch_handle(handle, http_client=ctx.http_client, rotator=ctx.rotator)
        for tm in matches:
            opp = tweet_to_opportunity(tm, source_id=ctx.source_id)
            await _publish_with_dedupe(ctx.q, opp, handle=handle, tweet_id=tm.tweet_id)


# ---- run() decomposition ---------------------------------------------------


def _log_boot_handles(handles: list[str]) -> None:
    """Emit boot-time handle inventory logs (byte-identical to pre-refactor)."""
    if not handles:
        _log.info("tw_no_handles_configured")
    else:
        _log.info("tw_handles_configured", count=len(handles), handles=handles)


async def _run_one_iteration(ctx: PollContext | None, *, source_id: int | None) -> None:
    """One poll cycle: re-read prefs, walk handles, swallow per-cycle errors.

    Re-reads prefs each loop so the user can append handles without a
    worker restart. Idles if no handles or no source_id is configured.
    Logging keys (`tw_idle_tick`, `tw_poll_error`) are byte-identical to
    the pre-refactor module.
    """
    current_handles = load_handles_from_prefs()
    if not current_handles or source_id is None or ctx is None:
        _log.debug(
            "tw_idle_tick",
            reason="no_handles" if not current_handles else "no_source_id",
        )
        await asyncio.sleep(_IDLE_SLEEP_SECONDS)
        return

    try:
        await _poll_once(current_handles, ctx)
    except (asyncio.CancelledError, KeyboardInterrupt):
        raise
    except Exception as e:
        _log.exception("tw_poll_error", err=str(e))

    await asyncio.sleep(_POLL_INTERVAL_SECONDS)


def _build_http_client() -> httpx.AsyncClient:
    """Construct the worker's shared httpx client (12 s timeout, hop UA)."""
    headers = {"User-Agent": _HTTP_USER_AGENT, "Accept": "text/html"}
    return httpx.AsyncClient(
        timeout=_HTTP_TIMEOUT,
        headers=headers,
        follow_redirects=True,
    )


async def _resolve_source() -> int | None:
    """Resolve the twitter source row id, logging the missing case (warn)."""
    source_id = await resolve_source_id()
    if source_id is None:
        _log.warning("tw_source_id_missing", strategy="twitter_founder_signal")
    return source_id


async def _poll_loop(ctx: PollContext | None, *, source_id: int | None) -> None:
    """Forever-loop driver. Delegates one cycle at a time to `_run_one_iteration`."""
    while True:
        await _run_one_iteration(ctx, source_id=source_id)


def _maybe_ctx(
    source_id: int | None,
    q: RedisQ,
    client: httpx.AsyncClient,
    rotator: _MirrorRotator,
    budget: _DailyBudget,
) -> PollContext | None:
    """PollContext when we have a source row; None signals the idle path."""
    if source_id is None:
        return None
    return PollContext(source_id=source_id, q=q, http_client=client, rotator=rotator, budget=budget)


async def run() -> None:
    """Worker entrypoint. Idempotent + restart-safe."""
    _log.info("tw_fetcher_started", mirrors=NITTER_INSTANCES)
    _log_boot_handles(load_handles_from_prefs())

    # DB + Redis come up regardless so health checks pass + the worker can
    # idle harmlessly when handles is empty (matches telegram_fetcher).
    await init_pool()
    q = await RedisQ.connect()
    source_id = await _resolve_source()
    rotator = _MirrorRotator(NITTER_INSTANCES)
    budget = _DailyBudget()

    try:
        async with _build_http_client() as client:
            ctx = _maybe_ctx(source_id, q, client, rotator, budget)
            await _poll_loop(ctx, source_id=source_id)
    except (asyncio.CancelledError, KeyboardInterrupt):
        _log.info("tw_shutdown")
    finally:
        await close_pool()


# Re-export for tests/back-compat. Tests touch `_publish_with_dedupe`,
# `parse_tweet_html`, `matches_hiring`, `infer_category`, `_fingerprint`,
# `tweet_to_opportunity`, `load_handles_from_prefs`, `_normalise_handle`,
# `_MirrorRotator`, `_DailyBudget`, `run`.
__all__: tuple[str, ...] = (
    "NITTER_INSTANCES",
    "PollContext",
    "TweetMatch",
    "fetch_handle",
    "infer_category",
    "load_handles_from_prefs",
    "matches_hiring",
    "parse_tweet_html",
    "resolve_source_id",
    "run",
    "tweet_to_opportunity",
)
