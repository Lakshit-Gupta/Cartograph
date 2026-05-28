"""Application sender - thin entry point.

Routes each apply to one of two pipelines via ``MP_RESUME_LATEX_ENABLED``:

- LaTeX path  -> :mod:`src.application.sender_latex` (tailored PDF).
- Legacy path -> :mod:`src.application.sender_legacy` (JSON resume).

Profile + email-target helpers live here; DB persistence helpers
(``_upsert_application``, ``_transition_to_applied``, ``queue_followup``)
live in :mod:`.sender_db` and are re-exported below for callers that
import them from ``src.application.sender``.

Phase 1 rule: EMAIL sends via Resend; non-EMAIL methods stash a
"review-then-click" link. Every send transitions ``opp.state ->
'applied'`` and publishes a ``NotificationTask`` onto ``Streams.NOTIFY``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

from src.common.db import current_tenant, fetch_one
from src.common.logger import get_logger
from src.common.secrets import get_settings

# Re-export the persistence helpers so existing callers
# (``sender_latex.pipeline``, ``sender_legacy``, ``followup``) keep
# importing them from ``src.application.sender`` unchanged.
from .sender_db import (
    ensure_followups_table as _ensure_followups_table,
)
from .sender_db import (
    queue_followup,
)
from .sender_db import (
    transition_to_applied as _transition_to_applied,  # noqa: F401  - re-exported
)
from .sender_db import (
    upsert_application as _upsert_application,  # noqa: F401  - re-exported
)

_log = get_logger(__name__)

_MAILTO_RE = re.compile(r"mailto:([^\?\s]+)", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")


def is_latex_enabled() -> bool:
    """LaTeX subsystem feature flag (``MP_RESUME_LATEX_ENABLED``)."""
    return bool(getattr(get_settings(), "mp_resume_latex_enabled", False))


# --- Profile loaders -------------------------------------------------------
def _profile_dir() -> Path:
    return Path(get_settings().config_root) / "profile"


def _load_profile_dict() -> dict[str, Any]:
    path = _profile_dir() / "resume.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _log.warning("resume_json_missing", path=str(path))
        return {}


def _load_prefs() -> dict[str, Any]:
    path = _profile_dir() / "prefs.yaml"
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}


def _profile_summary(profile_dict: dict[str, Any]) -> dict[str, Any]:
    basics = profile_dict.get("basics", {}) or {}
    return {
        "name": basics.get("name"),
        "email": basics.get("email"),
        "phone": basics.get("phone"),
        "location": basics.get("location"),
        "links": basics.get("links"),
        "summary": basics.get("summary"),
        "skills": profile_dict.get("skills"),
        "projects": [
            {"name": p.get("name"), "url": p.get("url"), "summary": p.get("summary")} for p in (profile_dict.get("projects") or [])
        ],
    }


# --- LaTeX tree paths (imported by applier.py warmup + sender_latex) ------
def _resume_root() -> Path:
    return Path(get_settings().config_root) / "profile" / "my_resume"


def _manifest_path() -> Path:
    return _resume_root() / "manifest.yaml"


# --- Opp loader -----------------------------------------------------------
async def _load_opp(opp_id: UUID) -> dict[str, Any] | None:
    rec = await fetch_one(
        """
        SELECT id, source_id, canonical_url, title, company, description,
               comp_min, comp_max, comp_currency, comp_period,
               location, remote_type, category, posted_at, expires_at,
               apply_url, apply_method, state
        FROM opportunities
        WHERE id = $1
        """,
        opp_id,
    )
    return dict(rec) if rec is not None else None


async def _load_opp_or_raise(opp_id: UUID) -> dict[str, Any]:
    opp = await _load_opp(opp_id)
    if opp is None:
        raise ValueError(f"opportunity not found: {opp_id}")
    return opp


def _load_profile_bundle() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return (profile_dict, profile_summary, prefs)."""
    profile_dict = _load_profile_dict()
    return profile_dict, _profile_summary(profile_dict), _load_prefs()


# --- Apply target resolution ----------------------------------------------
def _extract_email_target(apply_url: str | None, description: str | None) -> str | None:
    """Pull a mailto address out of apply_url, falling back to description."""
    for src in (apply_url or "", description or ""):
        m = _MAILTO_RE.search(src)
        if m:
            return m.group(1).strip()
    for src in (apply_url or "", description or ""):
        m = _EMAIL_RE.search(src)
        if m:
            return m.group(0).strip()
    return None


def _render_email_html(
    cover_md: str,
    bullets: list[str],
    opp: dict[str, Any],
    profile_summary: dict[str, Any],
) -> str:
    bullets_html = "".join(f"<li>{b}</li>" for b in bullets)
    name = profile_summary.get("name") or ""
    links = profile_summary.get("links") or {}
    portfolio = links.get("portfolio") or links.get("github") or ""
    cover_html = cover_md.replace("\n\n", "</p><p>").replace("\n", "<br/>")
    _ = opp  # kept for signature compatibility with legacy callers
    return (
        '<div style="font-family:system-ui,sans-serif;font-size:14px;line-height:1.5">'
        f"<p>{cover_html}</p><hr/>"
        "<p><strong>Relevant highlights:</strong></p>"
        f"<ul>{bullets_html}</ul>"
        f"<p>- {name}<br/>{portfolio}</p></div>"
    )


# --- Public API -----------------------------------------------------------
async def _dispatch_send_path(
    opp_id: UUID,
    opp: dict[str, Any],
    profile_dict: dict[str, Any],
    profile_summary: dict[str, Any],
    prefs: dict[str, Any],
    *,
    override_cover_markdown: str | None,
) -> dict[str, Any]:
    """Route to LaTeX or legacy path. LaTeX failures drop to legacy."""
    if is_latex_enabled():
        try:
            from .sender_latex import send_with_latex

            return await send_with_latex(
                opp_id,
                opp,
                profile_dict,
                profile_summary,
                prefs,
                current_tenant(),
                override_cover_markdown=override_cover_markdown,
            )
        except Exception as e:
            # Never silently swallow an apply: if the LaTeX path raises
            # before it can reach its own fallback (e.g. parser crash on
            # a malformed manifest), drop to legacy.
            _log.exception("latex_apply_failed_falling_back_to_json", err=str(e), opp_id=str(opp_id))

    from .sender_legacy import send_with_json_template

    return await send_with_json_template(
        opp_id,
        opp,
        profile_dict,
        profile_summary,
        prefs,
        override_cover_markdown=override_cover_markdown,
        user_id=current_tenant(),
    )


async def send_application(
    opp_id: UUID,
    *,
    override_cover_markdown: str | None = None,
) -> dict[str, Any]:
    """Tailor + send (or queue for manual review) + record + notify.

    Branches on :func:`is_latex_enabled`. ``override_cover_markdown``
    (e.g. user-edited freelance proposal) is used verbatim when given.
    """
    await _ensure_followups_table()
    opp = await _load_opp_or_raise(opp_id)
    profile_dict, profile_summary, prefs = _load_profile_bundle()
    return await _dispatch_send_path(
        opp_id,
        opp,
        profile_dict,
        profile_summary,
        prefs,
        override_cover_markdown=override_cover_markdown,
    )


__all__ = ["queue_followup", "send_application"]
