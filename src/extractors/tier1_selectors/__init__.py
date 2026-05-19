"""Per-source CSS-selector / JSON-shape extractors. Fall back to tier-2 LLM."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from src.extractors.base import ExtractInput, ExtractOutput

# Lazy registry — keyed by source slug.
_REGISTRY: dict[str, Callable[[ExtractInput], Awaitable[ExtractOutput]]] = {}


def register(slug: str):
    def deco(fn: Callable[[ExtractInput], Awaitable[ExtractOutput]]):
        _REGISTRY[slug] = fn
        return fn

    return deco


def get(slug: str) -> Callable[[ExtractInput], Awaitable[ExtractOutput]] | None:
    return _REGISTRY.get(slug)


# Eager-import selector modules so they register themselves
from . import (  # noqa: F401,E402  # noqa: F401,E402
    ashby,
    bounty_algora,
    bounty_gitcoin,
    bounty_replit,
    bounty_superteam,
    contra,
    cuvette,
    github_md,
    greenhouse,
    hn_algolia,
    internshala,
    lever,
    reddit_forhire,
    rss_generic,
    unstop,
    workable,
)
