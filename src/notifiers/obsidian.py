"""Obsidian vault writer.

Append-mode daily markdown notes. One file per day (`YYYY-MM-DD-opps.md`),
one ## section per opportunity. The notifier worker mounts the vault dir
into the container; default `/vault` in dev, `/mnt/storage/obsidian_vault`
on the Pi.
"""
from __future__ import annotations

import asyncio
import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from src.common.logger import get_logger

_log = get_logger(__name__)


def _fmt_money(opp: dict[str, Any]) -> str:
    lo, hi, cur = opp.get("comp_min"), opp.get("comp_max"), opp.get("comp_currency") or ""
    period = opp.get("comp_period") or ""
    if lo is None and hi is None:
        return "comp: —"
    if lo is not None and hi is not None and lo != hi:
        return f"comp: {cur}{lo:g}–{cur}{hi:g}/{period}".strip("/")
    val = lo if lo is not None else hi
    return f"comp: {cur}{val:g}/{period}".strip("/")


def _opp_section(opp: dict[str, Any], score: float | None = None) -> str:
    title = opp.get("title") or "(untitled)"
    company = opp.get("company") or "—"
    location = opp.get("location") or "—"
    remote = opp.get("remote_type") or "unspecified"
    category = opp.get("category") or "unknown"
    url = opp.get("canonical_url") or opp.get("apply_url") or ""
    posted_at = opp.get("posted_at") or "—"
    score_str = f" — score {score:.2f}" if score is not None else ""

    desc = (opp.get("description") or "").strip()
    if len(desc) > 800:
        desc = desc[:800].rstrip() + "…"

    return (
        f"\n## {title} — {company}{score_str}\n\n"
        f"- url: <{url}>\n"
        f"- {_fmt_money(opp)}\n"
        f"- location: {location} ({remote})\n"
        f"- category: {category}\n"
        f"- posted_at: {posted_at}\n"
        f"\n{desc}\n"
    )


async def write_opp_note(
    opp_dict: dict[str, Any],
    dest_dir: str | Path = "/vault",
    *,
    score: float | None = None,
    today: date | None = None,
) -> Path:
    """Append a section about a single opp to today's daily note."""
    today = today or datetime.now(UTC).date()
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"{today.isoformat()}-opps.md"

    section = _opp_section(opp_dict, score=score)
    is_new = not path.exists()

    def _write() -> None:
        with path.open("a", encoding="utf-8") as fh:
            if is_new:
                fh.write(f"# {today.isoformat()} — Marked_Path opps\n")
            fh.write(section)
            os.fsync(fh.fileno())

    try:
        await asyncio.to_thread(_write)
        _log.info("obsidian_appended", path=str(path), title=opp_dict.get("title"))
    except Exception as e:
        _log.exception("obsidian_write_failed", err=str(e), path=str(path))
        raise
    return path
