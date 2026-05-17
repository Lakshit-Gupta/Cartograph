"""Embed + thread helpers for `manual_apply_ready` NOTIFY kind.

Used for ATS_FORM / EMBEDDED_FORM / IN_PLATFORM / EXTERNAL applications where
sender.py prepared a cover letter + tailored bullets but couldn't auto-submit.
The user reviews in #✅-applied and clicks Mark applied / Cancel.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import discord

_AMBER = 0xF59E0B


def _truncate(text: str | None, n: int) -> str:
    if not text:
        return "—"
    text = str(text).strip()
    if len(text) <= n:
        return text
    return text[: n - 1].rstrip() + "…"


def thread_title(title: str | None, company: str | None) -> str:
    """`[REVIEW] <title> @ <company>`, total length ≤ 90 chars."""
    prefix = "[REVIEW] "
    t = (title or "(untitled)").strip()
    c = (company or "—").strip()
    full = f"{prefix}{t} @ {c}"
    if len(full) <= 90:
        return full
    # Trim title; preserve prefix + company.
    spare = 90 - len(prefix) - len(c) - 3  # ' @ '
    if spare < 8:
        return full[:90]
    return f"{prefix}{t[: spare - 1].rstrip()}… @ {c}"


def build_manual_apply(payload: dict[str, Any]) -> discord.Embed:
    """Amber embed with apply_url, tailored bullets, code-block cover letter."""
    title = payload.get("title") or "(untitled)"
    company = payload.get("company") or "—"
    apply_url = payload.get("apply_url") or payload.get("review_url") or payload.get("target") or ""
    bullets = payload.get("tailored_bullets") or []
    cover_md = payload.get("cover_letter_markdown") or ""

    embed = discord.Embed(
        title=_truncate(f"[REVIEW] {title} @ {company}", 256),
        color=discord.Color(_AMBER),
        timestamp=datetime.now(UTC),
    )
    embed.set_author(name=_truncate(str(company), 256))

    url_str = _truncate(str(apply_url), 256) if apply_url else "—"
    if apply_url and str(apply_url).startswith(("http://", "https://")):
        link_md = f"[Open]({url_str})"
        embed.add_field(name="Open the form", value=link_md[:1024], inline=False)
    else:
        embed.add_field(name="Open the form", value=url_str, inline=False)

    if bullets:
        joined = "\n".join(f"• {b}" for b in bullets if b)
        embed.add_field(
            name="Tailored bullets",
            value=_truncate(joined, 1024),
            inline=False,
        )

    if cover_md:
        snippet = cover_md[:1000]
        embed.add_field(
            name="Cover letter",
            value=f"```\n{snippet}\n```"[:1024],
            inline=False,
        )

    embed.set_footer(text="Review → submit on the site → click Mark applied")
    return embed


def chunk_cover_letter(cover_md: str, *, max_len: int = 1900) -> list[str]:
    """Split cover letter into ≤2000-char Discord messages (with code fence overhead).

    Splits on paragraph breaks (`\\n\\n`) where possible.
    """
    if not cover_md:
        return []
    text = cover_md.strip()
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    paragraphs = text.split("\n\n")
    cur = ""
    for p in paragraphs:
        addition = (p if not cur else "\n\n" + p)
        if len(cur) + len(addition) <= max_len:
            cur += addition
            continue
        if cur:
            chunks.append(cur)
        if len(p) <= max_len:
            cur = p
        else:
            # Single paragraph too long: hard-split.
            for i in range(0, len(p), max_len):
                chunks.append(p[i : i + max_len])
            cur = ""
    if cur:
        chunks.append(cur)
    return chunks


__all__ = ["build_manual_apply", "chunk_cover_letter", "thread_title"]
