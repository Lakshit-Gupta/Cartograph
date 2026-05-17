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
    FETCH = "stream:fetch"               # FetchTask
    EXTRACT = "stream:extract"           # FetchResult
    RANK = "stream:rank"                 # Opportunity
    NOTIFY = "stream:notify"             # RankedOpportunity / NotificationTask
    APPLY = "stream:apply"               # apply commands from buttons
    EMAIL_INBOUND = "stream:email_in"    # Gmail watcher → state writer
    ALERTS = "stream:alerts"             # system alerts
    DLQ = "stream:dlq"                   # dead-letter for any handler


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
        try:
            await self._r.xgroup_create(stream, group, id="$", mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def publish(self, stream: str, payload: dict[str, Any]) -> str:
        return await self._r.xadd(stream, {"data": json.dumps(payload, default=str)})

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
                reclaimed = await self._r.xautoclaim(
                    stream, group, self._consumer_name, min_idle_time=idle_reclaim_ms, count=count
                )
                if reclaimed and len(reclaimed) >= 2:
                    _, messages = reclaimed[0], reclaimed[1]
                    for msg_id, fields in messages or []:
                        yield StreamMessage(msg_id=msg_id, fields=_decode(fields))
            except Exception as e:
                _log.warning("xautoclaim_failed", err=str(e))

            try:
                resp = await self._r.xreadgroup(
                    group, self._consumer_name,
                    streams={stream: ">"}, block=block_ms, count=count,
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
        await self._r.xadd(
            Streams.DLQ,
            {"data": json.dumps({"src": src_stream, "msg_id": msg_id, "payload": payload, "err": err}, default=str)},
        )


def _decode(fields: dict[str, Any]) -> dict[str, Any]:
    if "data" in fields:
        try:
            return json.loads(fields["data"])
        except Exception:
            return fields
    return fields
