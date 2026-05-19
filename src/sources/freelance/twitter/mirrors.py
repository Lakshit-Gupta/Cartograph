"""Nitter mirror rotation + per-mirror cool-down state.

Per CLAUDE.md: each Nitter mirror gets a minimum 30s gap between requests
so we play nice with operators. The rotator naturally rotates away from
4xx/5xx mirrors because `cool()` pushes their `next_ok` forward.
"""

from __future__ import annotations

import time

# Canonical Nitter mirrors. Curated list of currently-reachable instances;
# updates land here (not in config) because the upstream wiki rotates fast
# and we keep the worker import-time deterministic. If the entire list goes
# dark the worker logs `tw_all_mirrors_failed` per poll cycle and idles.
NITTER_INSTANCES: tuple[str, ...] = (
    "https://nitter.net",
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
)

# Per-mirror minimum gap between requests, to play nice with operators.
_PER_MIRROR_MIN_GAP_SECONDS = 30.0


class _MirrorRotator:
    """Round-robin mirror picker that enforces a per-mirror cool-down.

    The simple invariant: each mirror's *next* fetch may not begin earlier
    than (last_fetched + _PER_MIRROR_MIN_GAP_SECONDS). On 4xx/5xx we mark
    the mirror as cooled for the same gap; the rotator naturally rotates
    away to the next healthy one.
    """

    def __init__(self, mirrors: tuple[str, ...]) -> None:
        self._mirrors = list(mirrors)
        # monotonic timestamps; 0.0 = never used.
        self._next_ok: dict[str, float] = {m: 0.0 for m in mirrors}

    def pick(self) -> str | None:
        """Return the mirror with the earliest next_ok <= now; else None.

        Caller can `await asyncio.sleep(rotator.wait_hint())` then retry.
        """
        now = time.monotonic()
        # Sort by readiness, ascending — earliest-ready wins.
        in_order = sorted(self._mirrors, key=lambda m: self._next_ok.get(m, 0.0))
        head = in_order[0]
        if self._next_ok.get(head, 0.0) <= now:
            return head
        return None

    def cool(self, mirror: str, *, gap: float = _PER_MIRROR_MIN_GAP_SECONDS) -> None:
        self._next_ok[mirror] = time.monotonic() + gap

    def wait_hint(self) -> float:
        """Seconds until the soonest mirror is ready. 0 if one is ready now."""
        now = time.monotonic()
        soonest = min(self._next_ok.values()) if self._next_ok else now
        return max(0.0, soonest - now)
