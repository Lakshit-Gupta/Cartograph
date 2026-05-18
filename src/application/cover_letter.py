"""Cover-letter writer.

Picks a template from `config/profile/cover_letters/` based on opp category
+ variant + location hints, then asks the LLM-writer to fill placeholders with
content drawn strictly from <PROFILE> + <OPP>. Template structure is preserved
verbatim; only placeholders may be replaced.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from src.common.llm import chat_json, fence_untrusted, load_prompt
from src.common.logger import get_logger
from src.common.secrets import get_settings
from src.common.types import OppCategory, Opportunity

_log = get_logger(__name__)

_AVAILABLE_TEMPLATES = {
    "generic",
    "backend",
    "fullstack",
    "ml",
    "freelance",
    "contract",
    "fellowship",
    "intern_india",
}

_INDIA_HINTS = (
    "india",
    "bangalore",
    "bengaluru",
    "hyderabad",
    "pune",
    "mumbai",
    "delhi",
    "ncr",
    "gurgaon",
    "noida",
    "chennai",
    "kolkata",
    "ahmedabad",
)


def _cover_dir() -> Path:
    return Path(get_settings().config_root) / "profile" / "cover_letters"


def _opp_field(opp: Opportunity | dict[str, Any], name: str) -> Any:
    if isinstance(opp, dict):
        return opp.get(name)
    return getattr(opp, name, None)


def _opp_text_blob(opp: Opportunity | dict[str, Any]) -> str:
    parts = [
        _opp_field(opp, "title"),
        _opp_field(opp, "company"),
        _opp_field(opp, "description"),
        _opp_field(opp, "location"),
    ]
    return " ".join(str(p) for p in parts if p).lower()


def _category_str(opp: Opportunity | dict[str, Any]) -> str:
    raw = _opp_field(opp, "category")
    if raw is None:
        return OppCategory.UNKNOWN.value
    if isinstance(raw, OppCategory):
        return raw.value
    return str(raw).lower()


def pick_template(opp: Opportunity | dict[str, Any], variant_label: str | None = None) -> str:
    """Return the filename (without .md) of the best-fit cover-letter template."""
    cat = _category_str(opp)
    blob = _opp_text_blob(opp)
    title = (str(_opp_field(opp, "title") or "")).lower()

    # Hard category routes first
    if cat == OppCategory.FELLOWSHIP.value or "fellowship" in title:
        return "fellowship"
    if cat == OppCategory.FREELANCE.value:
        return "freelance"
    if cat == OppCategory.CONTRACT.value or "contract" in title:
        return "contract"

    is_intern = cat == OppCategory.INTERNSHIP.value or "intern" in title
    in_india = any(h in blob for h in _INDIA_HINTS)
    if is_intern and in_india:
        return "intern_india"

    # Variant-driven defaults for fulltime / unknown
    if variant_label in {"backend", "fullstack", "ml"}:
        return variant_label

    # Final fallback
    return "generic"


def _read_template(name: str) -> str:
    if name not in _AVAILABLE_TEMPLATES:
        _log.warning("template_not_in_allowlist", name=name)
        name = "generic"
    path = _cover_dir() / f"{name}.md"
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        _log.warning("template_missing_fallback_generic", missing=str(path))
        return (_cover_dir() / "generic.md").read_text(encoding="utf-8")


def _opp_summary_for_prompt(opp: Opportunity | dict[str, Any]) -> dict[str, Any]:
    desc = (str(_opp_field(opp, "description") or ""))[:1500]
    return {
        "title": _opp_field(opp, "title"),
        "company": _opp_field(opp, "company"),
        "location": _opp_field(opp, "location"),
        "remote_type": str(_opp_field(opp, "remote_type") or ""),
        "category": _category_str(opp),
        "comp_min": _opp_field(opp, "comp_min"),
        "comp_max": _opp_field(opp, "comp_max"),
        "comp_currency": _opp_field(opp, "comp_currency"),
        "description": desc,
        "apply_url": _opp_field(opp, "apply_url"),
    }


_MD_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\n?|\n?```$", re.MULTILINE)


def _strip_fences(text: str) -> str:
    return _MD_FENCE_RE.sub("", text).strip()


async def write_cover(
    profile_summary: dict[str, Any] | str,
    opp: Opportunity | dict[str, Any],
    variant_label: str,
) -> str:
    """Fill cover-letter template using LLM. Returns markdown."""
    template_name = pick_template(opp, variant_label=variant_label)
    template_md = _read_template(template_name)

    prompt_template = load_prompt("cover_letter.txt")
    profile_json = profile_summary if isinstance(profile_summary, str) else json.dumps(profile_summary)[:3000]
    opp_json = json.dumps(_opp_summary_for_prompt(opp))

    system = prompt_template.format(
        profile_summary="<see PROFILE block>",
        opp_summary="<see OPP block>",
        resume_variant_label=variant_label,
        template_markdown="<see TEMPLATE block>",
    )

    user = (
        f"<PROFILE>{fence_untrusted(profile_json)}</PROFILE>\n"
        f"<OPP>{fence_untrusted(opp_json)}</OPP>\n"
        f"<VARIANT>{variant_label}</VARIANT>\n"
        f"<TEMPLATE>\n{template_md}\n</TEMPLATE>\n\n"
        'Return JSON: {"markdown": "<filled letter as markdown>"} and nothing else.'
    )

    try:
        data = await chat_json(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            kind="llm_writer",
            model=get_settings().openrouter_model_writer,
            max_tokens=900,
            temperature=0.3,
        )
    except Exception as e:
        _log.warning("write_cover_llm_failed", err=str(e), template=template_name)
        return template_md  # surface raw template — user can hand-edit

    md = data.get("markdown") if isinstance(data, dict) else None
    if not isinstance(md, str) or not md.strip():
        _log.warning("write_cover_bad_shape", template=template_name)
        return template_md
    return _strip_fences(md)
