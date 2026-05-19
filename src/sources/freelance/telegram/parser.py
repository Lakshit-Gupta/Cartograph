"""Pure Telegram message parser + Opportunity adapter.

All pure: zero network, zero DB. Safe to import at module-load time even
when Telethon is missing. Behaviour MUST stay byte-identical to the
original `telegram_fetcher.py` — the compensation regexes, fingerprint
hashing, and ParsedMessage shape are load-bearing across Grafana
dashboards and downstream consumers.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from src.common.types import ApplyMethod, OppCategory, Opportunity, RemoteType

# ----- sizing caps -----------------------------------------------------------

# Telegram message size — Telegram caps at 4096 chars, we cap at 2000 in DB.
_DESC_MAX = 2000
_TITLE_MAX = 200

# Confidence score for tier-0 structured Telegram extraction. Higher than the
# generic regex floor because channel posts follow a predictable shape.
_TG_EXTRACTION_CONFIDENCE = 0.6

# ----- compensation regexes --------------------------------------------------

# Compensation hints. Order matters: hourly first (more specific), then range,
# then bare USD, then bare INR. Each returns (lo, hi, currency, period).
_RX_HOURLY_USD = re.compile(r"\$\s?(\d[\d,]*)(?:\s*[-–]\s*\$?(\d[\d,]*))?\s*/\s*hr", re.I)
_RX_HOURLY_INR = re.compile(r"(?:₹|INR)\s?(\d[\d,]*)(?:\s*[-–]\s*(?:₹|INR)?(\d[\d,]*))?\s*/\s*hr", re.I)
_RX_RANGE_USD = re.compile(r"\$\s?(\d[\d,]*)\s*[-–to]+\s*\$?\s?(\d[\d,]*)")
_RX_RANGE_INR = re.compile(r"(?:₹|INR)\s?(\d[\d,]*)\s*[-–to]+\s*(?:₹|INR)?\s?(\d[\d,]*)")
_RX_BARE_USD = re.compile(r"\$\s?(\d[\d,]*)")
_RX_BARE_INR = re.compile(r"(?:₹\s?(\d[\d,]*)|(\d[\d,]*)\s*INR)", re.I)

_EMPTY_COMP: dict[str, Any] = {
    "comp_min": None,
    "comp_max": None,
    "comp_currency": None,
    "comp_period": None,
}


@dataclass(frozen=True, slots=True)
class ParsedMessage:
    """Structured view of a Telegram channel message used for `Opportunity`."""

    title: str
    description: str
    canonical_url: str
    fingerprint_hash: str
    comp_min: float | None
    comp_max: float | None
    comp_currency: str | None
    comp_period: str | None


# ----- pure helpers (unit-tested without Telethon) ---------------------------


def _normalise_channel(handle: str) -> str:
    """Accept `@foo`, `t.me/foo`, `https://t.me/foo`, or `foo` — emit `foo`."""
    h = handle.strip()
    if not h:
        return ""
    if h.startswith("http"):
        h = h.split("t.me/", 1)[-1]
    elif h.startswith("t.me/"):
        h = h[len("t.me/") :]
    return h.lstrip("@").rstrip("/")


def _fingerprint(channel: str, message_id: int) -> str:
    """sha256(channel:message_id) — deterministic, restart-safe."""
    return hashlib.sha256(f"{channel}:{message_id}".encode()).hexdigest()


def _strip_emoji_noise(text: str) -> str:
    """Drop zero-width joiners + variation selectors; collapse blank runs.

    Telegram-channel boilerplate is heavy on emoji and decorative dashes.
    Keep emoji characters (they often carry context) but normalise spacing.
    """
    cleaned = text.replace("‍", "").replace("️", "")
    # Collapse 3+ newlines into 2 (paragraph break).
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _first_nonempty_line(text: str, *, cap: int = _TITLE_MAX) -> str:
    for raw in text.splitlines():
        line = raw.strip(" \t•○●-—–*#>")
        if line:
            return line[:cap]
    return ""


def _to_float(s: str) -> float:
    """Strip thousands separators and cast to float."""
    return float(s.replace(",", ""))


def _hourly_match(text: str, rx: re.Pattern[str], currency: str) -> dict[str, Any] | None:
    m = rx.search(text)
    if not m:
        return None
    lo = _to_float(m.group(1))
    hi = _to_float(m.group(2)) if m.group(2) else lo
    return {"comp_min": lo, "comp_max": hi, "comp_currency": currency, "comp_period": "hour"}


def _range_match(text: str, rx: re.Pattern[str], currency: str) -> dict[str, Any] | None:
    m = rx.search(text)
    if not m:
        return None
    return {
        "comp_min": _to_float(m.group(1)),
        "comp_max": _to_float(m.group(2)),
        "comp_currency": currency,
        "comp_period": None,
    }


def _bare_usd_match(text: str) -> dict[str, Any] | None:
    m = _RX_BARE_USD.search(text)
    if not m:
        return None
    v = _to_float(m.group(1))
    return {"comp_min": v, "comp_max": v, "comp_currency": "USD", "comp_period": None}


def _bare_inr_match(text: str) -> dict[str, Any] | None:
    m = _RX_BARE_INR.search(text)
    if not m:
        return None
    v = _to_float(m.group(1) or m.group(2))
    return {"comp_min": v, "comp_max": v, "comp_currency": "INR", "comp_period": None}


def _parse_compensation(text: str) -> dict[str, Any]:
    """Best-effort rate extraction. Order: hourly USD/INR → range → bare."""
    extractors = (
        lambda t: _hourly_match(t, _RX_HOURLY_USD, "USD"),
        lambda t: _hourly_match(t, _RX_HOURLY_INR, "INR"),
        lambda t: _range_match(t, _RX_RANGE_USD, "USD"),
        lambda t: _range_match(t, _RX_RANGE_INR, "INR"),
        _bare_usd_match,
        _bare_inr_match,
    )
    for extractor in extractors:
        result = extractor(text)
        if result is not None:
            return result
    return dict(_EMPTY_COMP)


def parse_message(*, channel: str, message_id: int, text: str) -> ParsedMessage | None:
    """Pure parser. Returns None when the text is empty or has no title."""
    if not text or not text.strip():
        return None
    cleaned = _strip_emoji_noise(text)
    title = _first_nonempty_line(cleaned)
    if not title:
        return None
    description = cleaned[:_DESC_MAX]
    comp = _parse_compensation(cleaned)
    return ParsedMessage(
        title=title,
        description=description,
        canonical_url=f"https://t.me/{channel}/{message_id}",
        fingerprint_hash=_fingerprint(channel, message_id),
        **comp,
    )


def build_opportunity(parsed: ParsedMessage, *, source_id: int) -> Opportunity:
    """Wrap a `ParsedMessage` in the canonical `Opportunity` payload."""
    return Opportunity(
        source_id=source_id,
        canonical_url=parsed.canonical_url,
        title=parsed.title,
        company=None,  # not surfaced in channel posts
        description=parsed.description,
        comp_min=parsed.comp_min,
        comp_max=parsed.comp_max,
        comp_currency=parsed.comp_currency,
        comp_period=parsed.comp_period,
        location=None,
        remote_type=RemoteType.REMOTE,
        category=OppCategory.FREELANCE,
        posted_at=None,
        apply_url=parsed.canonical_url,
        apply_method=ApplyMethod.EXTERNAL,
        fingerprint_hash=parsed.fingerprint_hash,
        extraction_tier=0,
        extraction_confidence=_TG_EXTRACTION_CONFIDENCE,
    )
