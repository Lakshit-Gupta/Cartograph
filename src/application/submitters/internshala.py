"""Pi-side Internshala submitter.

Does NOT drive a browser. Reads the compiled tailored PDF from disk,
base64-encodes it, packages it with the cover letter + Q&A defaults +
identity hint, and publishes a `BrowserApplyTask` onto
`Streams.APPLY_BROWSER`. The actual Easy Apply click + form fill +
submit happens on the ThinkPad sidecar in
`src/workers/apply_browser_worker.py` -> `src/application/submitters/
internshala_browser.py`.

Why split this way:
  - The Pi already holds the compiled PDF (LaTeX pipeline writes it to
    /var/lib/agent/resume_artifacts/<user>/<opp>.complete/...). Encoding
    + publishing is cheap. Driving Firefox on the Pi would burn the IP
    + the 8GB RAM ceiling.
  - The sidecar has the master libsodium key + clean egress IP +
    headroom. It loads the identity row from SSH-tunneled Postgres on
    its own, so the Pi never reads decrypted cookies into memory.
  - A single producer (the Pi) keeps the per-day cap enforceable. If
    apply_browser_worker scaled past 1 replica, two consumers would
    parallel-submit the same identity and trip Internshala's
    cookie-rotation defence.

Phase 1 scope: Internshala only. Naukri / Cuvette / Unstop / Contra
land as sibling files in this directory under the same registry pattern.
"""

from __future__ import annotations

import base64
import uuid
from pathlib import Path
from typing import Any

import yaml

from src.common.logger import get_logger
from src.common.queue import RedisQ, Streams
from src.common.secrets import get_settings

from . import SubmitOutcome, register

_log = get_logger(__name__)


def _load_qa_defaults() -> dict[str, str]:
    """Read `config/profile/internshala_q_a.yaml`.

    Empty dict if the file is missing — the sidecar will skip any
    custom-question textareas that aren't covered by a key. Missing
    answers are not a hard failure; Internshala lets you submit with
    blanks, and the recruiter sees a less-complete application but
    still receives it. Better than refusing to apply.
    """
    settings = get_settings()
    path = Path(settings.config_root) / "profile" / "internshala_q_a.yaml"
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        _log.warning("internshala_qa_yaml_read_failed", err=str(e), path=str(path))
        return {}
    if not isinstance(loaded, dict):
        _log.warning("internshala_qa_yaml_not_a_dict")
        return {}
    # Coerce all values to str. YAML may produce ints/lists for ambiguous keys.
    return {str(k): str(v) for k, v in loaded.items()}


def _encode_pdf(pdf_path: Path) -> tuple[str, str]:
    """Base64-encode the PDF + derive a stable filename for upload.

    Returns (b64_payload, filename). filename strips the .complete dir
    convention and re-anchors to the candidate's name when possible so
    Internshala records `Lakshit_Gupta_Resume.pdf` not `<opp_id>.pdf`.
    """
    raw = pdf_path.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    # Internshala accepts any .pdf name. Use a candidate-anchored one so the
    # recruiter doesn't see an opportunity_id leaking through.
    filename = "Resume.pdf"
    return b64, filename


class _InternshalaSubmitter:
    """Registered as `in_platform_internshala` in the submitter registry."""

    key = "in_platform_internshala"

    async def prepare(
        self,
        *,
        opp: dict[str, Any],
        profile_summary: dict[str, Any],
        cover_md: str,
        tailored_bullets: list[str],
        pdf_path: Path | None,
        dry_run: bool,
        user_id: int,
    ) -> SubmitOutcome:
        if pdf_path is None or not pdf_path.exists():
            _log.warning(
                "internshala_submit_no_pdf",
                opp_id=str(opp.get("id")),
                pdf_path=str(pdf_path) if pdf_path else None,
            )
            return SubmitOutcome(
                status="failed",
                error="no compiled PDF available (LaTeX pipeline failed or fallback unavailable)",
            )

        try:
            pdf_b64, pdf_filename = _encode_pdf(pdf_path)
        except OSError as e:
            _log.exception("internshala_pdf_read_failed", err=str(e), path=str(pdf_path))
            return SubmitOutcome(status="failed", error=f"pdf read failed: {e}")

        task_id = uuid.uuid4().hex
        candidate_name = (profile_summary.get("name") or "Applicant").strip()

        payload = {
            "task_id": task_id,
            "platform": "internshala",
            "user_id": user_id,
            "opportunity_id": str(opp.get("id")),
            "apply_url": opp.get("apply_url"),
            "thread_title": f"{opp.get('title', '?')} @ {opp.get('company', '?')}",
            "pdf_b64": pdf_b64,
            "pdf_filename": pdf_filename,
            "cover_letter_md": cover_md,
            "tailored_bullets": tailored_bullets,
            "qa_defaults": _load_qa_defaults(),
            "candidate_name": candidate_name,
            "candidate_email": profile_summary.get("email"),
            "candidate_phone": profile_summary.get("phone"),
            "dry_run": bool(dry_run),
        }

        queue = await RedisQ.connect()
        await queue.publish(Streams.APPLY_BROWSER, payload)

        _log.info(
            "internshala_apply_browser_task_published",
            task_id=task_id,
            opp_id=str(opp.get("id")),
            dry_run=dry_run,
            apply_url=opp.get("apply_url"),
        )
        return SubmitOutcome(status="deferred", task_id=task_id)


# Self-register at import time. The submitters package's __init__ imports
# this module after defining `register`, so the side effect lands on a fully
# initialised registry.
submitter = register(_InternshalaSubmitter())


__all__ = ["submitter"]
