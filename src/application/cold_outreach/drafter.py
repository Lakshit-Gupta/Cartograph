"""LLM-driven cold intro email drafter.

Output contract — strictly JSON, single object:
    {
        "subject": "Brief subject under 60 chars",
        "body":    "Plain-text body, max 90 words"
    }

The drafter:

- Wraps all UNTRUSTED inputs (recipient bio, title, mission summary) in
  the `<IGNORE>…</IGNORE>` sentinels from `src.common.llm.fence_untrusted`
  so a prompt-injection attempt can't escape its scope.
- Calls `chat_json` with `kind="llm_writer"` so the cost ledger attributes
  spend correctly and the daily-cap circuit breaker fires if abused.
- Hard-trims the body to 90 words client-side. If the LLM overruns we
  truncate at word 90 and append "…" so the recipient sees a clean cut.
- Returns FAIL_DRAFT (None) on any error — the orchestrator skips the
  send rather than risk attaching a stale draft.

The prompt lives at `config/prompts/cold_outreach_drafter.txt` so the
user can tune voice + opening style without touching code.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.application.cold_outreach.base import Contact
from src.application.cold_outreach.sanitizer import scrub_text, word_count
from src.common.llm import (
    LLMEmptyResponse,
    LLMInvalidJSON,
    LLMSafetyBlock,
    chat_json,
    fence_untrusted,
    load_prompt,
)
from src.common.logger import get_logger
from src.common.secrets import get_settings

_log = get_logger(__name__)

MAX_BODY_WORDS = 90
MAX_SUBJECT_CHARS = 60


@dataclass(frozen=True)
class Draft:
    subject: str
    body: str


def _truncate_words(body: str, max_words: int) -> str:
    words = body.split()
    if len(words) <= max_words:
        return body.strip()
    return " ".join(words[:max_words]).rstrip(",;:") + " …"


def _build_prompt(
    *,
    profile_headline: str,
    profile_skills: list[str],
    company_name: str,
    mission_summary: str,
    why_target: str,
    contact: Contact,
) -> tuple[str, str]:
    """Return (system_prompt, user_prompt) pair.

    The system prompt is loaded from disk to keep voice tweakable. The user
    prompt fences every untrusted field so a malicious bio cannot smuggle
    instructions to the model.
    """
    try:
        system = load_prompt("cold_outreach_drafter.txt")
    except FileNotFoundError:
        # Fallback inline so the worker never hard-crashes if the prompt
        # file is missing on disk; we log loudly so the user knows to fix
        # the deployment.
        _log.warning("cold_outreach_prompt_file_missing")
        system = (
            "You write short cold intro emails (max 90 words). Output JSON: "
            '{"subject":"...","body":"..."}. The subject must be '
            "specific, under 60 chars, and DIFFERENT from any generic "
            "templates. Do NOT use the words 'opportunity', 'circling back', "
            "or 'just checking in'. Reference the company's mission in one "
            "concrete sentence. Close with a single question."
        )

    safe_bio = fence_untrusted(scrub_text(contact.bio))
    safe_title = fence_untrusted(scrub_text(contact.title, max_len=80))
    safe_mission = fence_untrusted(scrub_text(mission_summary))
    safe_why = fence_untrusted(scrub_text(why_target))
    safe_name = fence_untrusted(scrub_text(contact.name, max_len=80))
    safe_company = fence_untrusted(scrub_text(company_name, max_len=80))
    skills_csv = ", ".join(profile_skills[:8]) if profile_skills else "(unspecified)"

    user = (
        f"COMPANY: {safe_company}\n"
        f"MISSION (untrusted, fenced): {safe_mission}\n"
        f"WHY I'M REACHING OUT (untrusted, fenced): {safe_why}\n"
        f"RECIPIENT NAME: {safe_name}\n"
        f"RECIPIENT TITLE: {safe_title}\n"
        f"RECIPIENT BIO (untrusted, fenced): {safe_bio}\n"
        f"MY HEADLINE: {profile_headline}\n"
        f"MY SKILLS: {skills_csv}\n\n"
        f"Write the JSON now. Body MUST be <= {MAX_BODY_WORDS} words."
    )
    return system, user


async def draft_intro(
    *,
    profile_headline: str,
    profile_skills: list[str],
    company_name: str,
    mission_summary: str,
    why_target: str,
    contact: Contact,
) -> Draft | None:
    """Draft a single intro email. Returns None on any failure."""
    system, user = _build_prompt(
        profile_headline=profile_headline,
        profile_skills=profile_skills,
        company_name=company_name,
        mission_summary=mission_summary,
        why_target=why_target,
        contact=contact,
    )
    settings = get_settings()
    try:
        obj = await chat_json(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            kind="llm_writer",
            model=settings.openrouter_model_writer,
            schema_hint="object",
            temperature=0.5,
            max_tokens=400,
        )
    except (LLMEmptyResponse, LLMSafetyBlock, LLMInvalidJSON) as e:
        _log.warning("draft_llm_failed", err=str(e), kind=type(e).__name__)
        return None
    except Exception as e:  # cost cap, network, etc.
        _log.warning("draft_unexpected_error", err=str(e))
        return None

    if not isinstance(obj, dict):
        return None
    subject = scrub_text(str(obj.get("subject") or ""), max_len=MAX_SUBJECT_CHARS)
    body = str(obj.get("body") or "").strip()
    if not subject or not body:
        _log.warning("draft_missing_fields", has_subject=bool(subject), has_body=bool(body))
        return None
    body = _truncate_words(body, MAX_BODY_WORDS)
    # `_truncate_words` may append a single "…" sentinel when it had to
    # cut; count that as content-equivalent to the cap rather than an
    # overrun. Anything beyond +1 is a defensive refusal.
    wc = word_count(body)
    if wc > MAX_BODY_WORDS + 1:
        _log.warning("draft_body_exceeds_cap", words=wc)
        return None
    return Draft(subject=subject, body=body)
