"""Notifier Protocol — every concrete notifier accepts a dict payload and returns success."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Notifier(Protocol):
    """Implemented by Discord, Email, Obsidian, Telegram (future)."""

    name: str

    async def send(self, payload: dict[str, Any]) -> bool:
        """Send a notification. Return True on success, False on (logged) failure."""
        ...
