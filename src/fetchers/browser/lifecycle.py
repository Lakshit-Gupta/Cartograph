"""Page counter — recycle browser sessions every N pages to prevent RAM bloat."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class PageCounter:
    pages_rendered: int = 0
    sessions_spawned: int = 1

    def bump(self) -> None:
        self.pages_rendered += 1


def should_recycle(counter: PageCounter, limit: int) -> bool:
    return counter.pages_rendered >= limit
