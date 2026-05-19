"""Redis Streams wrapper. Subsystems communicate ONLY through this module."""

from __future__ import annotations

import asyncio
import json
import os
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from redis import asyncio as redis_async

from src.common.logger import get_logger
from src.common.secrets import get_settings

_log = get_logger(__name__)


# Canonical stream names (single source of truth)
class Streams:
    FETCH = "stream:fetch"  # FetchTask
    EXTRACT = "stream:extract"  # FetchResult
    RANK = "stream:rank"  # Opportunity
    NOTIFY = "stream:notify"  # RankedOpportunity / NotificationTask
    APPLY = "stream:apply"  # apply commands from buttons
    EMAIL_INBOUND = "stream:email_in"  # Gmail watcher → state writer
    ALERTS = "stream:alerts"  # system alerts
    DLQ = "stream:dlq"  # dead-letter for any handler


# Consumer groups
class Groups:
    CRAWLERS = "g:crawlers"
    EXTRACTORS = "g:extractors"
    RANKERS = "g:rankers"
    NOTIFIERS = "g:notifiers"
    APPLIERS = "g:appliers"
    EMAIL = "g:email"


@dataclass
class StreamMessage:
    msg_id: str
    fields: dict[str, Any]


class RedisQ:
    def __init__(self, client: redis_async.Redis):
        self._r = client
        self._consumer_name = f"{socket.gethostname()}-{os.getpid()}"

    @classmethod
    async def connect(cls) -> RedisQ:
        settings = get_settings()
        client: redis_async.Redis = redis_async.from_url(
            settings.redis_url,
            decode_responses=True,
            health_check_interval=30,
            retry_on_timeout=True,
        )
        await client.ping()
        _log.info("redis_connected", url=settings.redis_url.split("@")[-1])
        return cls(client)

    @property
    def raw(self) -> redis_async.Redis:
        return self._r

    async def ensure_group(self, stream: str, group: str) -> None:
        """Idempotent consumer-group bootstrap.

        WHY THE PROBE: `XGROUP CREATE ... MKSTREAM` is a WRITE command even
        when the group already exists — Redis rejects it under `noeviction`
        when `used_memory >= maxmemory` with `OutOfMemoryError`, *before*
        evaluating whether BUSYGROUP would have applied. That bug caused
        the entire crawler / camoufox-worker tier to restart-loop forever:
        every boot called `consume()` → `ensure_group()` → OOM → process
        exit → docker restart → same OOM. Symptom was indistinguishable
        from a "real" crash because the OOM trace landed inside the redis
        retry layer, not at the BUSYGROUP branch.

        FIX: probe with the read-only `XINFO GROUPS` first. The probe
        never writes, so it succeeds even when Redis is OOM. We only
        attempt the (write) `XGROUP CREATE` when the group is genuinely
        absent. If the write still fails with OOM (e.g. the stream itself
        does not exist yet so MKSTREAM is required), we tolerate it with
        bounded backoff so that as soon as another producer XADDs and
        Redis frees a slot we can advance — instead of restart-spinning.
        """
        # Fast path: probe with the read-only XINFO GROUPS. Works at OOM.
        try:
            groups = await self._r.xinfo_groups(stream)
            if any(g.get("name") == group for g in groups):
                return
        except Exception as e:
            # Stream missing → XINFO returns `no such key`. Fall through to
            # the create path; MKSTREAM will materialise the stream itself.
            if "no such key" not in str(e).lower():
                _log.warning("xinfo_groups_failed", stream=stream, err=str(e))

        # Group is absent (or stream is absent). Now we actually need a
        # write. Tolerate OOM with bounded backoff so the worker doesn't
        # die on cold-start memory pressure — once Redis frees a byte the
        # next attempt will succeed. Total bounded wait: ~7s (0.5+1+2+4).
        for attempt in range(4):
            try:
                await self._r.xgroup_create(stream, group, id="$", mkstream=True)
                return
            except Exception as e:
                msg = str(e)
                if "BUSYGROUP" in msg:
                    return
                if "OOM" in msg or "used memory" in msg.lower():
                    _log.warning(
                        "xgroup_create_oom_retry",
                        stream=stream,
                        group=group,
                        attempt=attempt,
                    )
                    await asyncio.sleep(0.5 * (2**attempt))
                    # Re-probe — a sibling worker may have created the
                    # group in the meantime, in which case we're done.
                    try:
                        groups = await self._r.xinfo_groups(stream)
                        if any(g.get("name") == group for g in groups):
                            return
                    except Exception:
                        pass
                    continue
                raise
        # Out of retries. Re-raise the last OOM so the supervisor can alert,
        # but only after we've spent ~7s — not the ~10ms previous behaviour.
        raise RuntimeError(
            f"ensure_group({stream=}, {group=}) failed after 4 OOM retries; Redis is at maxmemory cap. XTRIM streams or raise maxmemory."
        )

    # Per-stream MAXLEN caps. Redis is configured with maxmemory=200mb and
    # maxmemory-policy=noeviction (CLAUDE.md durability spec — under
    # back-pressure producers MUST block rather than silently lose data).
    # Without an XADD MAXLEN, streams grow until the 200MB cap is hit and
    # every publisher then sees OutOfMemoryError. The `~` modifier is an
    # approximate trim that lets Redis use whole-node deletion (cheap) at
    # the cost of leaving a few extra entries above the cap.
    #
    # Caps are sized for the bottleneck stage:
    #   FETCH   — produced by scheduler, consumed by crawlers (cheap)
    #   EXTRACT — consumed by extractor-worker (LLM-bound, slow). Smallest
    #             cap so back-pressure hits the crawler quickly instead of
    #             eating all of Redis.
    #   RANK    — consumed by ranker-worker (embedding-bound, medium).
    #   NOTIFY  — consumed by notifier-discord (network-bound).
    #   APPLY   — small, user-initiated.
    _MAXLEN: dict[str, int] = {
        "stream:fetch": 50_000,
        "stream:extract": 20_000,
        "stream:rank": 30_000,
        "stream:notify": 10_000,
        "stream:apply": 5_000,
        "stream:email_in": 10_000,
        "stream:alerts": 5_000,
        "stream:dlq": 50_000,
    }
    _DEFAULT_MAXLEN = 20_000

    async def publish(self, stream: str, payload: dict[str, Any]) -> str:
        maxlen = self._MAXLEN.get(stream, self._DEFAULT_MAXLEN)
        return await self._r.xadd(
            stream,
            {"data": json.dumps(payload, default=str)},
            maxlen=maxlen,
            approximate=True,
        )

    async def consume(
        self,
        stream: str,
        group: str,
        *,
        block_ms: int = 5_000,
        count: int = 10,
        idle_reclaim_ms: int = 5 * 60 * 1000,
    ) -> AsyncIterator[StreamMessage]:
        await self.ensure_group(stream, group)
        while True:
            # First: try to reclaim any pending messages stuck >= idle_reclaim_ms
            try:
                reclaimed = await self._r.xautoclaim(stream, group, self._consumer_name, min_idle_time=idle_reclaim_ms, count=count)
                if reclaimed and len(reclaimed) >= 2:
                    _, messages = reclaimed[0], reclaimed[1]
                    for msg_id, fields in messages or []:
                        yield StreamMessage(msg_id=msg_id, fields=_decode(fields))
            except Exception as e:
                _log.warning("xautoclaim_failed", err=str(e))

            try:
                resp = await self._r.xreadgroup(
                    group,
                    self._consumer_name,
                    streams={stream: ">"},
                    block=block_ms,
                    count=count,
                )
            except Exception as e:
                _log.exception("xreadgroup_failed", err=str(e))
                await asyncio.sleep(1)
                continue
            if not resp:
                continue
            for _, messages in resp:
                for msg_id, fields in messages:
                    yield StreamMessage(msg_id=msg_id, fields=_decode(fields))

    async def ack(self, stream: str, group: str, msg_id: str) -> None:
        await self._r.xack(stream, group, msg_id)

    async def dlq(self, src_stream: str, msg_id: str, payload: dict[str, Any], err: str) -> None:
        """Best-effort dead-letter sink.

        Callers reach this from an `except` branch — if dlq itself raises,
        the worker dies and docker restarts it, which is exactly the loop
        we are trying to avoid. Under Redis OOM (`maxmemory` cap +
        `noeviction`) XADD is rejected, so we MUST swallow OutOfMemoryError
        here and merely log it. The original message stays unacked in the
        source stream and will be re-delivered to a sibling consumer via
        `XAUTOCLAIM` once memory frees. Worst case: a buggy message gets
        re-tried until DLQ is writable again — but the worker stays alive.
        """
        try:
            await self._r.xadd(
                Streams.DLQ,
                {"data": json.dumps({"src": src_stream, "msg_id": msg_id, "payload": payload, "err": err}, default=str)},
                maxlen=self._MAXLEN.get(Streams.DLQ, self._DEFAULT_MAXLEN),
                approximate=True,
            )
        except Exception as e:
            # OOM is the expected failure under maxmemory pressure; anything
            # else is also non-fatal at this layer — the source message is
            # still in-flight on `src_stream` and will be reclaimed.
            _log.error(
                "dlq_write_failed",
                src_stream=src_stream,
                msg_id=msg_id,
                err=str(e),
                original_err=err,
            )


def _decode(fields: dict[str, Any]) -> dict[str, Any]:
    if "data" in fields:
        try:
            return json.loads(fields["data"])
        except Exception:
            return fields
    return fields
