"""Shared opp-row lookup used by `notify_applied` and `notify_manual_apply`.

Both handlers need the same `(title, company, apply_url)` triple for an
opportunity id. Extracting the lookup avoids the duplicated SELECT + dict
unpacking that previously inflated cyclomatic complexity in `bot.py`.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from src.common import db


async def resolve_opp_metadata(opp_id: Any) -> dict[str, Any]:
    """Return `{title, company, apply_url}` for the opportunity row, or {} when
    the id is falsy or the row doesn't exist. Mirrors the original inline
    lookup in `bot.py`; UUID parse errors bubble up (callers wrap in try/except).
    """
    if not opp_id:
        return {}
    row = await db.fetch_one(
        "SELECT title, company, apply_url FROM opportunities WHERE id = $1",
        UUID(str(opp_id)),
    )
    return dict(row) if row is not None else {}
