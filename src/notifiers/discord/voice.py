"""Microcopy bank. Keeps voice consistent across digest, push, nudges.

Use `pick(key)` for a random variant. Use `get(key, index=0)` for the
canonical/deterministic phrasing (handy for tests).

Bot identity: **Hop** (Grace Hopper — first compiler, 1952). Microcopy can
reference the name lightly via `BOT_NAME`. Don't over-personify; user wants
information delivery, not a chatty mascot.
"""
from __future__ import annotations

import random
from collections.abc import Iterable

BOT_NAME = "Hop"
BOT_TAGLINE = "hops 28+ sources for the leads worth your time"
EMBED_FOOTER_TEMPLATE = "Hop · {ts}"

_VOICE: dict[str, list[str]] = {
    "daily_digest_header": [
        "Today's stack",
        "Fresh pull",
        "Morning shortlist",
        "On the radar",
        "Hop's daily haul",
    ],
    "daily_digest_empty": [
        "Quiet day. No new opps cleared the bar.",
        "Nothing scored above your floor today.",
        "Slow tide. Pipeline alive, signal sparse.",
    ],
    "priority_push_header": [
        "Move fast",
        "Hot lead",
        "Time-sensitive",
        "Don't sleep on this one",
        "Hop says move",
    ],
    "applied_confirm": [
        "Logged.",
        "On the books.",
        "Application recorded.",
    ],
    "skipped_confirm": [
        "Skipped. Won't bother you again.",
        "Hidden from future digests.",
        "Filtered out.",
    ],
    "snoozed_confirm": [
        "Snoozed. We'll re-surface it later.",
        "Tucked away. Back soon.",
        "Hibernating.",
    ],
    "pinned_confirm": [
        "Pinned. Stays at the top.",
        "Locked in.",
    ],
    "nudge_below_target_9pm": [
        "{n} apps sent today. Target was {target}. One more before bed?",
        "Apply count: {n}/{target}. Worth one more push.",
        "{n}/{target} fired today. Easy to close the gap.",
    ],
    "source_quarantined": [
        "Source `{slug}` quarantined — repeated failures.",
        "Pausing `{slug}`. CF or ban signal sustained.",
    ],
    "identity_banned": [
        "Identity `{label}` flagged banned. Siblings auto-quarantined.",
    ],
    "cost_cap_reached": [
        "Daily LLM cap hit (${cap}). Deferring extractor LLM calls until midnight.",
    ],
    "explain_intro": [
        "Why this matched:",
        "Score breakdown:",
        "Here's the math:",
    ],
    "freelance_push": [
        "Freelance hot lead — proposal-ready.",
        "Speed lane: propose now or lose it.",
    ],
}


def get(key: str, index: int = 0) -> str:
    options = _VOICE.get(key) or [""]
    if not options:
        return ""
    return options[index % len(options)]


def pick(key: str, *, rng: random.Random | None = None) -> str:
    options = _VOICE.get(key) or [""]
    r = rng or random
    return r.choice(options) if options else ""


def keys() -> Iterable[str]:
    return _VOICE.keys()
