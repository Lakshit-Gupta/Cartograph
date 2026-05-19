"""Variant resolution helpers for the LaTeX apply path.

Two thin wrappers around ``src.application.resume_latex.variant_picker``:

- :func:`resolve_variant` - bandit pick, then keyword-vote fallback, then
  the prefs-driven default. Mirrors the legacy ``_send_with_latex``
  branch exactly.
- :func:`resolve_variant_db_id` - look up the V011 ``resume_variants.id``
  for a given label, returning ``None`` when V011 isn't applied yet.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from src.common.logger import get_logger

_log = get_logger(__name__)


async def resolve_variant(opp: dict[str, Any], prefs: dict[str, Any], opp_id: UUID) -> str:
    """Bandit -> keyword vote -> prefs default."""
    from src.application.resume_latex.variant_picker import pick_variant_async

    from ..resume_tailor import pick_variant

    try:
        bandit_label = await pick_variant_async(opp)
    except Exception as e:
        _log.warning(
            "variant_picker_failed_falling_back_to_keyword_vote",
            err=str(e),
            opp_id=str(opp_id),
        )
        bandit_label = ""
    fallback = (prefs.get("apply") or {}).get("resume_variant_default") or "backend"
    return bandit_label or pick_variant(opp) or fallback


async def resolve_variant_db_id(variant_label: str) -> int | None:
    """Lookup ``resume_variants.id`` for ``variant_label`` (V011)."""
    from src.application.resume_latex.variant_picker import variant_id_for_label

    try:
        return await variant_id_for_label(variant_label)
    except Exception as e:
        _log.warning("variant_id_lookup_failed", err=str(e), label=variant_label)
        return None


__all__ = ["resolve_variant", "resolve_variant_db_id"]
