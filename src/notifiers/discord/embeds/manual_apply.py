"""Embed + thread helpers for `manual_apply_ready` NOTIFY kind.

Used for ATS_FORM / EMBEDDED_FORM / IN_PLATFORM / EXTERNAL applications where
sender.py prepared a cover letter + tailored bullets but couldn't auto-submit.
The user reviews in #✅-applied and clicks Mark applied / Cancel.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import discord

# ---------------------------------------------------------------------------
# Layout constants — Discord embed + message limits.
# Public so downstream callers don't redefine the same magic numbers.
# ---------------------------------------------------------------------------

#: Amber/review color reused across `[REVIEW]` embeds.
EMBED_COLOR_AMBER = 0xF59E0B

#: Max length of a Discord embed title / author / inline URL value.
EMBED_TITLE_MAX = 256

#: Max length of a Discord embed field value.
EMBED_FIELD_VALUE_MAX = 1024

#: Soft cap on raw cover-letter text we slice into the embed preview.
#: The full text still rides in `chunk_cover_letter`; this is just the inline tease.
COVER_LETTER_PREVIEW_MAX = 1000

#: Max length of a forum / text-channel thread name.
THREAD_NAME_MAX = 90

#: Per-chunk cap for cover-letter messages. 1900 leaves headroom under the
#: Discord 2000-char message ceiling for the surrounding ``` fence + newlines.
COVER_LETTER_CHUNK_MAX = 1900

_ELLIPSIS = "…"
_THREAD_PREFIX = "[REVIEW] "
_DEFAULT_TITLE = "(untitled)"
_DEFAULT_COMPANY = "—"


def _truncate(text: str | None, n: int) -> str:
    if not text:
        return "—"
    text = str(text).strip()
    if len(text) <= n:
        return text
    return text[: n - 1].rstrip() + _ELLIPSIS


def thread_title(title: str | None, company: str | None) -> str:
    """`[REVIEW] <title> @ <company>`, total length ≤ ``THREAD_NAME_MAX``."""
    t = (title or _DEFAULT_TITLE).strip()
    c = (company or _DEFAULT_COMPANY).strip()
    full = f"{_THREAD_PREFIX}{t} @ {c}"
    if len(full) <= THREAD_NAME_MAX:
        return full
    # Trim title; preserve prefix + company.
    spare = THREAD_NAME_MAX - len(_THREAD_PREFIX) - len(c) - 3  # ' @ '
    if spare < 8:
        return full[:THREAD_NAME_MAX]
    return f"{_THREAD_PREFIX}{t[: spare - 1].rstrip()}{_ELLIPSIS} @ {c}"


def _resolve_apply_url(payload: dict[str, Any]) -> str:
    return payload.get("apply_url") or payload.get("review_url") or payload.get("target") or ""


def _open_form_field_value(apply_url: str) -> str:
    """Return the embed field value for the apply link.

    Renders as ``[Open](<url>)`` markdown for http(s) URLs, else the raw
    truncated string (or em-dash when missing). Discord auto-escapes embed
    field text so we can rely on that, but we still bound the length.
    """
    if not apply_url:
        return "—"
    url_str = _truncate(str(apply_url), EMBED_TITLE_MAX)
    if str(apply_url).startswith(("http://", "https://")):
        return f"[Open]({url_str})"[:EMBED_FIELD_VALUE_MAX]
    return url_str


def _format_bullets(bullets: list[str]) -> str | None:
    """Render tailored bullets into a single field value, or ``None`` if empty."""
    cleaned = [b for b in bullets if b]
    if not cleaned:
        return None
    joined = "\n".join(f"• {b}" for b in cleaned)
    return _truncate(joined, EMBED_FIELD_VALUE_MAX)


def _format_cover_letter_preview(cover_md: str) -> str | None:
    """Render the inline (code-block) cover-letter snippet, or ``None`` if empty."""
    if not cover_md:
        return None
    snippet = cover_md[:COVER_LETTER_PREVIEW_MAX]
    return f"```\n{snippet}\n```"[:EMBED_FIELD_VALUE_MAX]


def build_manual_apply(payload: dict[str, Any]) -> discord.Embed:
    """Amber embed with apply_url, tailored bullets, code-block cover letter."""
    title = payload.get("title") or _DEFAULT_TITLE
    company = payload.get("company") or _DEFAULT_COMPANY
    apply_url = _resolve_apply_url(payload)
    bullets = payload.get("tailored_bullets") or []
    cover_md = payload.get("cover_letter_markdown") or ""

    embed = discord.Embed(
        title=_truncate(f"{_THREAD_PREFIX}{title} @ {company}", EMBED_TITLE_MAX),
        color=discord.Color(EMBED_COLOR_AMBER),
        timestamp=datetime.now(UTC),
    )
    embed.set_author(name=_truncate(str(company), EMBED_TITLE_MAX))

    embed.add_field(
        name="Open the form",
        value=_open_form_field_value(apply_url),
        inline=False,
    )

    bullets_value = _format_bullets(bullets)
    if bullets_value is not None:
        embed.add_field(name="Tailored bullets", value=bullets_value, inline=False)

    cover_value = _format_cover_letter_preview(cover_md)
    if cover_value is not None:
        embed.add_field(name="Cover letter", value=cover_value, inline=False)

    embed.set_footer(text="Review → submit on the site → click Mark applied")
    return embed


def _hard_split_paragraph(p: str, max_len: int) -> list[str]:
    """Slice an over-long single paragraph into ``max_len`` slabs."""
    return [p[i : i + max_len] for i in range(0, len(p), max_len)]


def _append_paragraph(chunks: list[str], cur: str, paragraph: str, max_len: int) -> str:
    """Fold one paragraph into the running chunk, flushing when it overflows.

    Returns the new ``cur`` (in-progress chunk). Mutates ``chunks`` by
    appending whenever a flush is needed.
    """
    addition = paragraph if not cur else "\n\n" + paragraph
    if len(cur) + len(addition) <= max_len:
        return cur + addition
    if cur:
        chunks.append(cur)
    if len(paragraph) <= max_len:
        return paragraph
    chunks.extend(_hard_split_paragraph(paragraph, max_len))
    return ""


def chunk_cover_letter(cover_md: str, *, max_len: int = COVER_LETTER_CHUNK_MAX) -> list[str]:
    """Split cover letter into ≤2000-char Discord messages (with code fence overhead).

    Splits on paragraph breaks (``\\n\\n``) where possible.
    """
    if not cover_md:
        return []
    text = cover_md.strip()
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    cur = ""
    for p in text.split("\n\n"):
        cur = _append_paragraph(chunks, cur, p, max_len)
    if cur:
        chunks.append(cur)
    return chunks


__all__ = ["build_manual_apply", "chunk_cover_letter", "thread_title"]
