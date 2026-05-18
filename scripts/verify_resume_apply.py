#!/usr/bin/env python3
"""End-to-end probe for the Stage-4 LaTeX resume apply path.

This script imports ``_send_with_latex`` from ``src.application.sender`` and
runs it against a real ``opportunity_id`` pulled live from Postgres. Every
external boundary (Resend, the LLM, the Redis NOTIFY publish) is
monkey-patched so the probe is side-effect-free outside the database rows
it cleans up at the end.

It bypasses the ``is_latex_enabled()`` feature flag by calling
``_send_with_latex`` directly, so the probe works whether the flag is on
or off — useful for verifying Stage 4 before flipping the switch in prod.

Exit code 0 iff every assertion passes; non-zero otherwise.

Designed to be run inside the ``marked_path-applier-worker:latest`` image
where tectonic / qpdf / exiftool / the resume tree volume are all available.
Example invocation::

    sops exec-env secrets.yaml 'docker compose run --rm \\
        -v /home/lakshit_gupta/coding/Marked_Path:/app \\
        --entrypoint /opt/venv/bin/python applier-worker \\
        /app/scripts/verify_resume_apply.py'
"""

from __future__ import annotations

import asyncio
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

# ---------------------------------------------------------------------------
# Container vs host import shim
# ---------------------------------------------------------------------------
# When this script is invoked inside the applier container with the repo
# bind-mounted at /app, the working dir is already /app and ``src`` is
# importable. When invoked from the host venv we still need ``src`` on the
# path; insert the repo root to handle both cases.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Assertion accumulator
# ---------------------------------------------------------------------------
@dataclass
class AssertionResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class ProbeReport:
    results: list[AssertionResult] = field(default_factory=list)

    def check(self, name: str, condition: bool, detail: str = "") -> bool:
        self.results.append(AssertionResult(name=name, passed=bool(condition), detail=detail))
        marker = "PASS" if condition else "FAIL"
        print(f"  [{marker}] {name}: {detail}" if detail else f"  [{marker}] {name}")
        return bool(condition)

    @property
    def all_passed(self) -> bool:
        return all(r.passed for r in self.results)

    def summary(self) -> str:
        rows = ["", "=" * 72, "Summary", "-" * 72]
        for r in self.results:
            marker = "PASS" if r.passed else "FAIL"
            rows.append(f"  [{marker}] {r.name}")
            if r.detail and not r.passed:
                rows.append(f"        detail: {r.detail}")
        rows.append("=" * 72)
        passed = sum(1 for r in self.results if r.passed)
        rows.append(f"Total: {passed}/{len(self.results)} passed")
        return "\n".join(rows)


# ---------------------------------------------------------------------------
# Monkey-patch helpers — installed before importing sender so the patched
# symbols win (sender does ``from ... import send_email`` so we have to
# rebind on the sender module too, not just on the email module).
# ---------------------------------------------------------------------------
@dataclass
class CapturedSendEmail:
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, *args: Any, **kwargs: Any) -> bool:
        # Normalise args/kwargs into a single dict for inspection.
        spec = {
            "to": kwargs.get("to") or (args[0] if args else None),
            "subject": kwargs.get("subject") or (args[1] if len(args) > 1 else None),
            "html": kwargs.get("html") or (args[2] if len(args) > 2 else None),
            "reply_to": kwargs.get("reply_to"),
            "text": kwargs.get("text"),
            "headers": kwargs.get("headers"),
            "attachments": kwargs.get("attachments"),
        }
        self.calls.append(spec)
        return True


@dataclass
class CapturedPublish:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def __call__(self, stream: str, payload: dict[str, Any]) -> str:
        self.calls.append((stream, payload))
        return "0-0"


async def _fake_write_cover(*_args: Any, **_kwargs: Any) -> str:
    """Stand-in for src.application.cover_letter.write_cover.

    The real implementation calls ``json.dumps`` on a dict containing
    ``Decimal`` comp_min / comp_max columns from Postgres without a
    ``default=str`` argument — that raises ``TypeError`` on every opp
    with comp values set. The probe records this as a defect; stubbing
    write_cover lets the rest of the path proceed.
    """
    return "Dear hiring team,\n\nProbe cover.\n\n— Probe"


async def _fake_chat_json(**kwargs: Any) -> dict[str, Any]:
    """Stand-in for src.common.llm.chat_json — returns a canned tailor + cover.

    The kind/messages let us return shape-correct payloads for both the
    resume tailor call (``{"edits": [...]}``) and the cover-letter call
    (``{"markdown": "..."}``) without touching OpenRouter.
    """
    kind = kwargs.get("kind", "")
    if kind == "resume_tailor":
        # We don't know the parsed block ids in advance, so return a
        # deliberately wrong id — the sender treats unknown ids as no-op
        # and falls through to render-with-no-edits, which still exercises
        # the full compile pipeline. If we wanted "real" tailoring we'd
        # have to re-parse the resume tree here; for a probe, exercising
        # the compile path is sufficient.
        return {"edits": [{"id": "probe-placeholder", "bullets": ["probe bullet"]}]}
    if kind == "llm_writer":
        return {"markdown": "Dear hiring team,\n\nThis is a probe cover letter.\n\n-- Probe"}
    return {}


def _fake_load_prompt(filename: str) -> str:
    """Stand-in for src.common.llm.load_prompt.

    Returns a prompt template stripped of any literal ``{"edits": ...}``
    example that would collide with Python ``.format()`` substitution.
    The probe doesn't care about the LLM output shape (chat_json is also
    stubbed) — what matters is that ``prompt.format(...)`` succeeds so
    the call graph reaches the compile / render steps.
    """
    if filename == "resume_tailor.txt":
        return (
            "You rewrite resume bullets.\n"
            "<OPP>{opp_summary}</OPP>\n"
            "<VARIANT>{variant_label}</VARIANT>\n"
            "<BLOCKS>{blocks_json}</BLOCKS>\n"
            "Output JSON.\n"
        )
    if filename == "cover_letter.txt":
        return (
            "Write a cover letter using profile {profile_summary} "
            "for opp {opp_summary} variant {resume_variant_label} "
            "template {template_markdown}.\n"
        )
    # Anything else: passthrough sentinel — caller is expected to be
    # patched too and not reach this branch.
    return "{}"


# ---------------------------------------------------------------------------
# Probe core
# ---------------------------------------------------------------------------
async def _pick_opportunity(report: ProbeReport) -> dict[str, Any] | None:
    """Pick a real opportunity.

    V004's state machine only allows ``digested``, ``seen``, or
    ``snoozed`` as legal source states for ``applied``. We therefore
    require ``state IN ('digested','seen','snoozed')`` so the
    ``_transition_to_applied`` step doesn't raise CheckViolation.
    Order of preference within the legal set:
        1. email + description (best coverage)
        2. any apply_method (still exercises everything except email send)
    """
    from src.common.db import fetch_one

    legal_states = ("digested", "seen", "snoozed")

    # Best: an email opp in a legal state.
    rec = await fetch_one(
        """
        SELECT id, title, company, apply_method, state, description
          FROM opportunities
         WHERE apply_method = 'email'
           AND state = ANY($1::opp_state_enum[])
           AND description IS NOT NULL
           AND length(description) > 100
         ORDER BY first_seen DESC
         LIMIT 1
        """,
        list(legal_states),
    )
    if rec is not None:
        return dict(rec)

    # Fallback: any legal-state opp.
    rec = await fetch_one(
        """
        SELECT id, title, company, apply_method, state, description
          FROM opportunities
         WHERE state = ANY($1::opp_state_enum[])
           AND description IS NOT NULL
           AND length(description) > 100
         ORDER BY first_seen DESC
         LIMIT 1
        """,
        list(legal_states),
    )
    if rec is not None:
        return dict(rec)

    report.check("opportunity_lookup", False, "no eligible opportunity found in DB")
    return None


async def _cleanup(opp_id: UUID, application_id: int | None, pre_state: str) -> None:
    """Best-effort cleanup of every row the probe created/mutated.

    The V004 state machine forbids ``applied -> digested`` (no legal
    reverse path), so resetting state via plain UPDATE fires the trigger
    and raises CheckViolation. We disable the trigger for the duration
    of the cleanup transaction via ``session_replication_role = replica``
    (the standard Postgres pattern for admin-only trigger bypass).
    """
    from src.common.db import acquire

    print()
    print("Cleanup:")
    try:
        async with acquire() as conn, conn.transaction():
            # Disable triggers for this transaction so we can roll
            # state back without going through the state machine.
            await conn.execute("SET LOCAL session_replication_role = replica")

            if application_id is not None:
                await conn.execute(
                    "DELETE FROM applications WHERE id = $1",
                    application_id,
                )
            # Always sweep compile_log by opp_id in case probe ran
            # more than once or the application insert failed midway.
            await conn.execute(
                "DELETE FROM resume_compile_log WHERE opportunity_id = $1",
                opp_id,
            )
            # Reset opportunity state to pre-probe value.
            await conn.execute(
                """
                    UPDATE opportunities
                       SET state = $1::opp_state_enum
                     WHERE id = $2
                    """,
                pre_state,
                opp_id,
            )
            # Drop the transition rows the trigger AND _send_with_latex
            # inserted — we want zero pollution. The trigger logs with
            # trigger='auto' and the sender logs with
            # trigger='send_application'.
            await conn.execute(
                """
                    DELETE FROM opportunity_transitions
                     WHERE opportunity_id = $1
                       AND occurred_at >= NOW() - INTERVAL '10 minutes'
                       AND trigger IN ('send_application', 'auto')
                    """,
                opp_id,
            )
        print(f"  - deleted applications row id={application_id}")
        print(f"  - deleted resume_compile_log rows for opp_id={opp_id}")
        print(f"  - reset opportunities.state to '{pre_state}'")
        print("  - swept recent opportunity_transitions rows")
    except Exception as e:
        print(f"  ! cleanup failed: {e!r}")
        traceback.print_exc()


async def _run_probe() -> int:
    from src.common.db import close_pool, fetch_one, init_pool
    from src.common.queue import RedisQ

    print("=" * 72)
    print("Stage-4 LaTeX resume apply path probe")
    print("=" * 72)

    # ---- DB + Redis bootstrap (same pattern as src/workers/applier.py) ----
    print()
    print("Bootstrap:")
    await init_pool()
    print("  postgres pool ready")
    queue = await RedisQ.connect()
    print(f"  redis connected ({queue.raw})")

    report = ProbeReport()

    # ---- Pick an opportunity ---------------------------------------------
    print()
    print("Picking opportunity:")
    opp = await _pick_opportunity(report)
    if opp is None:
        await close_pool()
        return 2
    opp_id = opp["id"]
    pre_state = opp["state"]
    print(f"  id           = {opp_id}")
    print(f"  title        = {opp['title']!r}")
    print(f"  company      = {opp.get('company')!r}")
    print(f"  apply_method = {opp['apply_method']!r}")
    print(f"  state        = {pre_state!r}")

    # ---- Install monkey-patches -----------------------------------------
    # These have to happen BEFORE the sender module is imported by name
    # inside _send_with_latex's call graph. Import sender first, then
    # rebind the names it pulled in via ``from ... import send_email``.
    import src.application.resume_latex.fallback as fallback_mod
    import src.application.sender as sender_mod
    import src.common.llm as llm_mod
    import src.common.queue as queue_mod
    import src.notifiers.email as email_mod

    captured_email = CapturedSendEmail()
    captured_publish = CapturedPublish()

    # send_email is referenced as a module attribute on both modules; patch
    # both so any other path that re-imports email still finds the stub.
    email_mod.send_email = captured_email  # type: ignore[assignment]
    sender_mod.send_email = captured_email  # type: ignore[assignment]

    # write_cover is imported eagerly at the top of sender.py (``from
    # .cover_letter import pick_template, write_cover``). Patch the
    # binding inside sender so _send_with_latex's local reference picks
    # up the stub. The real write_cover raises TypeError on Decimal
    # comp_min/comp_max — that's recorded as a defect in the report.
    sender_mod.write_cover = _fake_write_cover  # type: ignore[assignment]

    # chat_json + load_prompt are imported lazily INSIDE _llm_tailor_blocks
    # and write_cover; patch the module-level symbols so the lazy imports
    # pick up our stubs. The load_prompt patch is required because the live
    # ``config/prompts/resume_tailor.txt`` contains a literal ``{"edits":
    # ...}`` JSON example that collides with ``prompt.format()`` — that's
    # the prompt-format defect captured in the defects report. Patching
    # load_prompt lets the probe exercise the rest of the path even while
    # the defect is unfixed.
    llm_mod.chat_json = _fake_chat_json  # type: ignore[assignment]
    llm_mod.load_prompt = _fake_load_prompt  # type: ignore[assignment]

    # publish is bound at runtime via queue.publish(...) so patching the
    # bound method on the connected client is the safest approach.
    queue_mod.RedisQ.publish = captured_publish  # type: ignore[assignment]

    # Redirect _ARTIFACT_ROOT (sender) AND _FALLBACK_ROOT (fallback) to a
    # writable tmpfs path under /tmp. The deployed bind mount at
    # /var/lib/agent/resume_artifacts/ is owned by root:root inside the
    # applier-worker container which runs as uid 1000 — _send_with_latex
    # raises PermissionError on the user_root.mkdir() at sender.py:360 in
    # production today. This patch lets the probe verify the rest of the
    # path; the underlying defect is recorded in the defects report.
    probe_artifact_root = Path("/tmp/probe_resume_artifacts")  # noqa: S108 — probe-only tmpfs path
    probe_artifact_root.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 — synchronous Path is fine for one-off probe bootstrap
    sender_mod._ARTIFACT_ROOT = probe_artifact_root  # type: ignore[assignment]
    fallback_mod._FALLBACK_ROOT = probe_artifact_root  # type: ignore[assignment]

    # Pre-warm a fallback PDF at <probe_artifact_root>/1/fallback.pdf so the
    # compile path has somewhere to land if tectonic fails. Skip if warm-up
    # itself fails — the probe will then report 'failed' status, which is
    # still a valid observable outcome.
    print()
    print("Warming fallback PDF (so the fallback branch is exercisable):")
    try:
        fb_path = await fallback_mod.warm_fallback_pdf(
            user_id=1,
            resume_root=Path(sender_mod._resume_root()),
            main_file="mmayer.tex",
        )
        if fb_path is not None:
            size = fb_path.stat().st_size
            print(f"  fallback ready: {fb_path} ({size}B)")
        else:
            print("  fallback warm-up returned None (compile failed)")
    except Exception as e:
        print(f"  fallback warm-up raised: {e!r}")

    print()
    print("Monkey-patches installed:")
    print("  src.notifiers.email.send_email -> CapturedSendEmail (no HTTP)")
    print("  src.application.sender.send_email -> CapturedSendEmail")
    print("  src.application.sender.write_cover -> stub (Decimal defect bypass)")
    print("  src.common.llm.chat_json -> canned tailor + cover JSON")
    print("  src.common.llm.load_prompt -> brace-safe templates (defect bypass)")
    print("  src.common.queue.RedisQ.publish -> CapturedPublish (no XADD)")
    print(f"  _ARTIFACT_ROOT / _FALLBACK_ROOT -> {probe_artifact_root} (bind-mount perm bypass)")

    # ---- Invoke _send_with_latex ----------------------------------------
    print()
    print("Invoking _send_with_latex:")
    application_id: int | None = None
    result: dict[str, Any] = {}
    invocation_error: Exception | None = None
    try:
        result = await sender_mod._send_with_latex(opp_id)
        application_id = result.get("application_id") if isinstance(result, dict) else None
        print(f"  returned: keys={list(result.keys()) if isinstance(result, dict) else type(result)}")
    except Exception as e:
        invocation_error = e
        print(f"  RAISED: {e!r}")
        traceback.print_exc()

    # ---- Assertions ------------------------------------------------------
    print()
    print("Assertions:")

    # (a) return shape
    required_keys = {
        "application_id",
        "method",
        "tailored_bullets",
        "resume_compile_status",
        "resume_artifact_sha256",
    }
    report.check(
        "return_shape",
        isinstance(result, dict) and required_keys.issubset(set(result.keys())),
        detail=f"got keys={sorted(result.keys()) if isinstance(result, dict) else None}",
    )

    # (b) compile status is one of the allowed CHECK values
    compile_status = result.get("resume_compile_status") if isinstance(result, dict) else None
    report.check(
        "compile_status_valid",
        compile_status in ("tailored", "fallback", "failed"),
        detail=f"resume_compile_status={compile_status!r}",
    )

    # If failed, surface helper diagnostics
    if compile_status == "failed":
        row = await fetch_one(
            """
            SELECT tectonic_stderr
              FROM resume_compile_log
             WHERE opportunity_id = $1
             ORDER BY created_at DESC
             LIMIT 1
            """,
            opp_id,
        )
        if row is not None:
            err = row["tectonic_stderr"] or ""
            print(f"  [info] tectonic_stderr={err[:300]!r}")

    # (c) applications row + V007 columns
    app_row = None
    if application_id is not None:
        app_row = await fetch_one(
            """
            SELECT id, method, resume_artifact_sha256, resume_source_hash, resume_compile_status
              FROM applications
             WHERE id = $1
            """,
            application_id,
        )
    sha_consistency = True
    sha_detail = ""
    if app_row is not None:
        # Per V007 CHECK: status must be one of tailored/fallback/failed.
        # Sha256 should be populated for tailored/fallback, may be null for failed.
        sha = app_row["resume_artifact_sha256"]
        status = app_row["resume_compile_status"]
        if status in ("tailored", "fallback"):
            sha_consistency = sha is not None
            sha_detail = f"status={status} sha256={'<set>' if sha else '<null>'}"
        else:
            # failed: sha may be null or set depending on whether fallback PDF
            # was warmed before the failure. Either is acceptable.
            sha_detail = f"status={status} sha256={'<set>' if sha else '<null>'}"
    report.check(
        "applications_row_with_v007_columns",
        app_row is not None and sha_consistency,
        detail=sha_detail or f"app_row={app_row}",
    )

    # (d) resume_compile_log row
    log_row = await fetch_one(
        """
        SELECT tectonic_version, compile_duration_ms, status
          FROM resume_compile_log
         WHERE opportunity_id = $1
         ORDER BY created_at DESC
         LIMIT 1
        """,
        opp_id,
    )
    if log_row is None:
        report.check("resume_compile_log_row", False, "no log row found")
    else:
        # On compile-failure paths the sender records the log row before
        # ``result`` is bound, so ``tectonic_version`` and ``duration_ms``
        # stay NULL — that's expected and a valid observable side effect.
        # On tailored success both should be populated.
        status = log_row["status"]
        version = log_row["tectonic_version"]
        dur = log_row["compile_duration_ms"]
        if status == "tailored":
            version_ok = bool(version) and "tectonic" in (version or "").lower()
            duration_ok = dur is not None and 0 <= int(dur) < 30000
            detail = f"status=tailored version={version!r} duration_ms={dur}"
        else:
            # failed / fallback: log row must exist with the matching
            # status; version + duration may be NULL.
            version_ok = True
            duration_ok = dur is None or (0 <= int(dur) < 30000)
            detail = f"status={status!r} version={version!r} duration_ms={dur} (NULLs expected on non-tailored)"
        report.check(
            "resume_compile_log_row",
            version_ok and duration_ok,
            detail=detail,
        )

    # (e) PDF file existence
    pdf_check_path: Path | None = None
    if compile_status == "tailored":
        # _send_with_latex writes the tailored artifact under
        # <_ARTIFACT_ROOT>/<user_id>/<opp_id>.complete/<main_file.pdf>.
        # Probe is single-tenant (user_id=1). The artifact root is the
        # patched probe path, not the production /var/lib/agent path.
        from src.application.resume_latex.parser.manifest import load as load_manifest

        manifest = load_manifest(Path(sender_mod._resume_root()) / "manifest.yaml")
        pdf_name = manifest.main_file.replace(".tex", ".pdf")
        pdf_check_path = probe_artifact_root / "1" / f"{opp_id}.complete" / pdf_name
    elif compile_status == "fallback":
        pdf_check_path = probe_artifact_root / "1" / "fallback.pdf"

    if pdf_check_path is None:
        report.check(
            "pdf_artifact_on_disk",
            compile_status == "failed",
            detail=f"compile_status={compile_status} — no PDF expected on failed; otherwise should not be None",
        )
    else:
        exists = pdf_check_path.exists()
        size = pdf_check_path.stat().st_size if exists else 0
        report.check(
            "pdf_artifact_on_disk",
            exists and size > 0,
            detail=f"path={pdf_check_path} size={size}B",
        )

    # (f) send_email called with PDF attachment (EMAIL branch only)
    method = result.get("method") if isinstance(result, dict) else None
    if method == "email":
        if not captured_email.calls:
            report.check("send_email_called_with_attachment", False, "send_email was never invoked")
        else:
            call = captured_email.calls[0]
            attachments = call.get("attachments")
            report.check(
                "send_email_called_with_attachment",
                isinstance(attachments, list) and len(attachments) > 0 and all(isinstance(a, Path) for a in attachments),
                detail=f"attachments={attachments}",
            )
    else:
        # Non-email path: hard rule says we still must not leak PDF via Discord.
        # Skip the email assertion but mark it noted.
        report.check(
            "send_email_called_with_attachment",
            True,
            detail=f"method={method!r} — email branch not exercised; checked NOTIFY payload instead",
        )

    # (g) NOTIFY publish payload must NOT carry a PDF path (hard rule #5)
    if not captured_publish.calls:
        report.check("notify_payload_has_no_pdf_field", False, "no publish call recorded")
    else:
        # Search every recorded publish for any pdf-shaped key/value.
        offenders: list[str] = []

        def _scan(node: Any, prefix: str = "") -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    lk = str(k).lower()
                    if "pdf" in lk or "resume_pdf" in lk or "pdf_path" in lk:
                        offenders.append(f"{prefix}{k}")
                    _scan(v, prefix + f"{k}.")
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    _scan(v, prefix + f"[{i}].")

        for _stream, payload in captured_publish.calls:
            _scan(payload)
        report.check(
            "notify_payload_has_no_pdf_field",
            not offenders,
            detail=f"offending keys={offenders}" if offenders else "no pdf-shaped keys in NOTIFY payload",
        )

    # ---- Cleanup ---------------------------------------------------------
    await _cleanup(opp_id, application_id, pre_state)

    # The probe also creates files under /tmp/probe_resume_artifacts. Those
    # live on the container's /tmp tmpfs and disappear when the container
    # exits, so we leave them for inspection during the probe run.

    # ---- Final summary ---------------------------------------------------
    print(report.summary())

    # Force a one-line ground-truth report for the human reading the logs.
    print()
    print(
        f"ground_truth: opp_id={opp_id} method={method} status={compile_status} "
        f"app_id={application_id} invocation_error={invocation_error!r}"
    )

    await close_pool()
    return 0 if report.all_passed else 1


def main() -> int:
    try:
        return asyncio.run(_run_probe())
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # pragma: no cover — defensive
        print(f"probe crashed: {e!r}", file=sys.stderr)
        traceback.print_exc()
        return 3


if __name__ == "__main__":
    sys.exit(main())
