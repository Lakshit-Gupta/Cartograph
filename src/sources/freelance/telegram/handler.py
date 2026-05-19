"""Telethon event-callback wiring.

`_handle_event` consumes one `NewMessage` event, parses it, and hands off
to a `publisher` callable supplied by the worker (typically
`telegram_fetcher._publish_with_dedupe`, which the test suite patches).
`_attach_handler` registers the on-event handler against the Telethon
client for the given channel set.

All logging keys (`tg_*`) are byte-identical to pre-refactor telegram_fetcher
because Grafana dashboards key on them.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from src.common.logger import get_logger
from src.common.queue import RedisQ
from src.common.types import Opportunity

from .parser import build_opportunity, parse_message

_log = get_logger(__name__)

# Publisher contract: (q, opp, *, channel, message_id) -> None.
Publisher = Callable[[RedisQ, Opportunity], Awaitable[None]]


async def _handle_event(
    event: Any,
    q: RedisQ,
    source_id: int,
    *,
    publish: Callable[[RedisQ, Opportunity, str, int], Awaitable[None]],
) -> None:
    """Parse one NewMessage event and publish. Errors logged, never raised."""
    try:
        chat = await event.get_chat()
        channel = getattr(chat, "username", None) or str(getattr(chat, "id", "unknown"))
        message_id = int(event.message.id)
        text = event.message.text or event.message.message or ""
        _log.info(
            "tg_message_received",
            channel=channel,
            message_id=message_id,
            length=len(text),
        )
        parsed = parse_message(channel=channel, message_id=message_id, text=text)
        if parsed is None:
            _log.debug("tg_skip_empty", channel=channel, message_id=message_id)
            return
        opp = build_opportunity(parsed, source_id=source_id)
        await publish(q, opp, channel, message_id)
    except Exception as e:
        _log.exception("tg_handler_failed", err=str(e))


def _attach_handler(
    client: Any,
    events: Any,
    *,
    channels: list[str],
    q: RedisQ,
    source_id: int,
    publish: Callable[[RedisQ, Opportunity, str, int], Awaitable[None]],
) -> None:
    """Attach NewMessage handler to the client for the given channel set."""

    @client.on(events.NewMessage(chats=channels))
    async def _handler(event: Any) -> None:
        await _handle_event(event, q, source_id, publish=publish)
