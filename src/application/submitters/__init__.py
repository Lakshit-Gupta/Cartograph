"""Per-method submitter registry — Phase 4 auto-apply router.

Each submitter implements `SubmitterProtocol` and registers itself under a
canonical key matching `prefs.auto_apply.methods`:

  email                       — EMAIL apply, Resend HTTP send (existing path
                                — see src/application/sender_latex/dispatch.py
                                _send_resend_email). NOT routed through this
                                module today; reserved for a future cleanup
                                that pulls the Resend send into a submitter
                                so the dispatch loop reads symmetrically.
  in_platform_internshala     — IN_PLATFORM apply on Internshala.
                                Publishes a BrowserApplyTask onto
                                stream:apply_browser for the spare-machine
                                sidecar to consume + drive camoufox.

Future entries (Phase 4.2+):
  in_platform_naukri / in_platform_cuvette / in_platform_unstop / in_platform_contra
  ats_form_greenhouse / ats_form_lever / ats_form_ashby / ats_form_workable

Hard rule: a submitter ONLY runs after `src/application/policy.py:
should_auto_submit()` has returned a `submit` or `submit_deferred_dryrun`
decision. The router does NOT re-check policy — that responsibility lives
in the caller (the LaTeX / legacy dispatch refactor).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class SubmitOutcome:
    """Return value of a submitter's prepare()/run() call.

    `status='deferred'` means the submit was dispatched onto a stream (the
    actual submission happens asynchronously on the sidecar / by a worker);
    the Pi-side caller should suppress the existing manual_apply_ready
    notify and wait for stream:apply_browser_result instead.

    `status='sent'` means the submission completed synchronously inside
    this process (e.g. Resend HTTP). Caller behaves as if the legacy EMAIL
    path ran.

    `status='failed'` lets the caller fall back to manual_apply_ready with
    the error in the embed.
    """

    status: str  # 'deferred' | 'sent' | 'failed'
    task_id: str | None = None
    error: str | None = None
    extra: dict[str, Any] | None = None


class SubmitterProtocol(Protocol):
    """Every per-method submitter implements this interface."""

    key: str  # registry key — must match prefs.auto_apply.methods entries

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
    ) -> SubmitOutcome: ...


# Lazy registry — submitters self-register at import time below.
_REGISTRY: dict[str, SubmitterProtocol] = {}


def register(submitter: SubmitterProtocol) -> SubmitterProtocol:
    """Decorator/idempotent function to register a submitter by its key."""
    existing = _REGISTRY.get(submitter.key)
    if existing is not None and existing is not submitter:
        # Re-import under test reload — replace silently. Production never
        # imports the same submitter module twice with distinct instances.
        pass
    _REGISTRY[submitter.key] = submitter
    return submitter


def resolve(key: str | None) -> SubmitterProtocol | None:
    """Look up a submitter by its canonical key. None when no match."""
    if key is None:
        return None
    return _REGISTRY.get(key)


def registered_keys() -> list[str]:
    """Snapshot of currently-registered keys. For diagnostics + tests."""
    return sorted(_REGISTRY.keys())


# Eager-import submitters so they self-register. Keep this import block at
# the BOTTOM so the public surface (register / resolve / Protocol) is fully
# defined before submitter modules import from this package. The reference
# below silences "unused import" for static analysers — the actual purpose
# is the import-time side effect that calls `register(...)`.
from . import internshala as _internshala  # noqa: E402

_LOADED = (_internshala,)


__all__ = [
    "SubmitOutcome",
    "SubmitterProtocol",
    "register",
    "registered_keys",
    "resolve",
]
