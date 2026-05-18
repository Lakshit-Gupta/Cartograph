"""Strategy → SourcePlugin registry. Workers look up by crawler_strategy column."""

from __future__ import annotations

from src.sources.base import SourcePlugin

_REGISTRY: dict[str, SourcePlugin] = {}


def register(plugin: SourcePlugin) -> SourcePlugin:
    _REGISTRY[plugin.strategy] = plugin
    return plugin


def get(strategy: str) -> SourcePlugin | None:
    return _REGISTRY.get(strategy)


def all_strategies() -> list[str]:
    return sorted(_REGISTRY.keys())


# Eager imports so plugins self-register
def _load() -> None:
    from src.sources import hn_algolia, reddit_forhire  # noqa: F401
    from src.sources.ats import ashby, greenhouse, lever, workable  # noqa: F401
    from src.sources.fellowship import (  # noqa: F401
        anthropic,
        cohere,
        huggingface,
        mats,
        ml_collective,
        openai_residency,
        yc_fellows,
    )
    from src.sources.freelance import contra, telegram_channel, upwork_email  # noqa: F401
    from src.sources.github_markdown import ouckah, simplifyjobs  # noqa: F401
    from src.sources.india import (  # noqa: F401
        cuvette,
        inc42,
        internshala,
        unstop,
        yc_india,
        yourstory,
    )
    from src.sources.rss import remoteok, weworkremotely  # noqa: F401


_load()
