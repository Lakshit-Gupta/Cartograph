"""Crawler worker — consumes Streams.FETCH, dispatches through tier chain, emits Streams.EXTRACT.

Owns the identity checkout / release lifecycle for sources whose
`sources.auth_account_id` is set (Pattern B from the Stage 2.2 design):
the dispatcher stays pure (no DB calls), the crawler hits the DB to look
up the auth platform, leases via `identity_vault.checkout`, splices
cookies + ua_string into the FetchRequest, and always releases in the
`finally` branch. On observed ban (HTTP 403/401, captcha markers, or
explicit `X-Identity-Banned` header) we call `identity_vault.mark_banned`
so sibling identities aren't routed to the same broken account. Empty
vault — the steady state until sock-puppets are seeded — degrades to
anonymous fetch with a single `identity_lease_missed` warning per source.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
from typing import TYPE_CHECKING

from src.common import identity_vault
from src.common.db import acquire, close_pool, init_pool
from src.common.logger import configure_logging, get_logger
from src.common.queue import Groups, RedisQ, Streams
from src.common.types import FetchResult, FetchTask
from src.fetchers.base import FetchRequest
from src.fetchers.dispatcher import TierDispatcher

if TYPE_CHECKING:
    from src.common.types import IdentityLease
    from src.fetchers.base import FetchResponse

configure_logging("crawler")
_log = get_logger(__name__)

# Status codes that indicate the leased identity is no longer trusted by the
# origin. 401 == credentials rejected (cookie expired / account locked).
# 403 == access denied; pair with CF marker check to avoid mis-flagging
# Cloudflare interstitials as identity bans.
_BAN_STATUS_CODES = frozenset({401, 403})
_BAN_HEADER = "X-Identity-Banned"
_CF_MARKERS = ("Attention Required", "Just a moment", "Checking your browser")

# Worker id is stable per process — used in `identity_checkouts.worker_id`
# so post-mortem audit can see exactly which container held the lease.
_WORKER_ID = f"crawler-{socket.gethostname()}-{os.getpid()}"


async def _lookup_auth_platform(source_id: int) -> str | None:
    """Return the platform string for the identity bound to a source, or None
    when the source is anonymous (`auth_account_id IS NULL`)."""
    async with acquire() as conn:
        rec = await conn.fetchrow(
            """
            SELECT i.platform
            FROM sources s
            JOIN identities i ON i.id = s.auth_account_id
            WHERE s.id = $1
            """,
            source_id,
        )
    if rec is None:
        return None
    return str(rec["platform"])


def _is_ban_signal(resp: FetchResponse) -> tuple[bool, str]:
    """Decide whether a response indicates the leased identity has been banned.

    Heuristic, documented in CLAUDE.md → Stage 2.2:
      - 401 / 403 status code unless the body carries a Cloudflare marker
        (CF challenges are not identity bans — they're route-layer
        interstitials that the cookie cache + tier escalation handles).
      - Explicit `X-Identity-Banned` response header (string truthy).

    Returns (banned?, reason).
    """
    if resp.headers and resp.headers.get(_BAN_HEADER):
        return True, f"header:{_BAN_HEADER}"
    if resp.status in _BAN_STATUS_CODES:
        if resp.cf_challenge_observed or any(m in (resp.body or "") for m in _CF_MARKERS):
            return False, ""
        return True, f"status_{resp.status}"
    return False, ""


async def _process(q: RedisQ, dispatcher: TierDispatcher, fields: dict) -> None:
    task = FetchTask.model_validate(fields)

    lease: IdentityLease | None = None
    platform = await _lookup_auth_platform(task.source_id)
    if platform is not None:
        lease = await identity_vault.checkout(platform=platform, worker_id=_WORKER_ID)
        if lease is None:
            _log.warning(
                "identity_lease_missed",
                source_id=task.source_id,
                source_slug=task.source_slug,
                platform=platform,
            )

    req = FetchRequest(
        source_id=task.source_id,
        source_slug=task.source_slug,
        url=task.url,
        identity_id=lease.identity_id if lease else None,
        cookies=lease.cookies if lease else None,
        ua_string=lease.ua_string if lease else None,
    )

    try:
        resp = await dispatcher.fetch(req, task.tier_chain)

        if lease is not None:
            banned, reason = _is_ban_signal(resp)
            if banned:
                await identity_vault.mark_banned(lease.identity_id, reason=reason)
                _log.warning(
                    "identity_banned_during_fetch",
                    source_id=task.source_id,
                    identity_id=lease.identity_id,
                    reason=reason,
                )

        result = FetchResult(
            source_id=task.source_id,
            source_slug=task.source_slug,
            url=task.url,
            http_status=resp.status,
            content=resp.body,
            content_type=resp.content_type,
            tier_used=resp.tier,
            correlation_id=task.correlation_id,
            error=resp.error,
        )
        await q.publish(Streams.EXTRACT, result.model_dump(mode="json"))
    finally:
        if lease is not None:
            await identity_vault.release(lease.lease_id)


async def main() -> None:
    await init_pool()
    q = await RedisQ.connect()
    dispatcher = TierDispatcher()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    _log.info("crawler_started")
    async for msg in q.consume(Streams.FETCH, Groups.CRAWLERS):
        if stop.is_set():
            break
        try:
            await _process(q, dispatcher, msg.fields)
        except Exception as e:
            _log.exception("crawler_process_failed", err=str(e))
            await q.dlq(Streams.FETCH, msg.msg_id, msg.fields, str(e))
        await q.ack(Streams.FETCH, Groups.CRAWLERS, msg.msg_id)

    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
