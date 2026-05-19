"""Resend HTTP client wrapper.

Direct REST call against https://api.resend.com/emails — we avoid the
`resend` SDK so we keep the dependency surface async-friendly.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.common.logger import get_logger
from src.common.metrics import deliver_success_total
from src.common.secrets import get_settings

_log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Resend HTTP constants.
# ---------------------------------------------------------------------------

_RESEND_URL = "https://api.resend.com/emails"

#: HTTP status thresholds (named so the regex linter stops yelling).
_HTTP_CLIENT_ERROR_MIN = 400
_HTTP_SERVER_ERROR_MIN = 500

#: Truncation budgets for log bodies (avoid blowing the logger up with HTML).
_LOG_BODY_TRUNCATE_5XX = 200
_LOG_BODY_TRUNCATE_4XX = 300

#: Retry / HTTP behaviour.
_HTTP_TIMEOUT_SEC = 20.0
_RETRY_ATTEMPTS = 3
_RETRY_WAIT_MIN = 1
_RETRY_WAIT_MAX = 10


class EmailSendError(RuntimeError):
    pass


def _build_resend_payload(
    *,
    from_email: str,
    to: str | list[str],
    subject: str,
    html: str,
) -> dict[str, Any]:
    """Compose the mandatory JSON fields for ``POST /emails``.

    Optional fields (text, reply_to, headers, attachments) are layered on
    separately by :func:`_apply_optional_fields` / :func:`_attach_files`.
    """
    return {
        "from": from_email,
        "to": [to] if isinstance(to, str) else list(to),
        "subject": subject,
        "html": html,
    }


def _apply_optional_fields(
    body: dict[str, Any],
    *,
    text: str | None,
    reply_to: str | None,
    headers: dict[str, str] | None,
) -> None:
    """Fold optional non-attachment fields onto ``body`` in-place."""
    if text is not None:
        body["text"] = text
    if reply_to:
        body["reply_to"] = reply_to
    if headers:
        body["headers"] = headers


def _attach_files(body: dict[str, Any], attachments: list[Path] | None) -> None:
    """Mutate ``body`` to add base64-encoded ``attachments``.

    Unreadable files are skipped with a warning; if every attachment fails
    we omit the field entirely (Resend rejects an empty array).
    """
    if not attachments:
        return
    encoded: list[dict[str, str]] = []
    for att in attachments:
        try:
            data = att.read_bytes()
        except OSError as e:
            _log.warning("attachment_read_failed", path=str(att), err=str(e))
            continue
        encoded.append(
            {
                "filename": att.name,
                "content": base64.b64encode(data).decode("ascii"),
            }
        )
    if encoded:
        body["attachments"] = encoded


def _auth_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _classify_resend_response(resp: httpx.Response, *, subject: str) -> bool | None:
    """Map a Resend HTTP response into the send-flow outcome.

    Returns:
        - ``True`` for a 2xx success.
        - ``False`` for a 4xx (non-retryable client error — already logged).
        - ``None`` to signal the caller should treat this as retryable
          (the caller raises :class:`EmailSendError` so tenacity replays).
    """
    if resp.status_code >= _HTTP_SERVER_ERROR_MIN:
        return None
    if resp.status_code >= _HTTP_CLIENT_ERROR_MIN:
        _log.error(
            "resend_4xx",
            status=resp.status_code,
            body=resp.text[:_LOG_BODY_TRUNCATE_4XX],
            subject=subject,
        )
        return False
    return True


async def _post_resend(body: dict[str, Any], req_headers: dict[str, str], subject: str) -> bool:
    """Single POST attempt — translates the response into the send-flow bool.

    Raises :class:`EmailSendError` on 5xx so the surrounding tenacity loop
    retries the request.
    """
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SEC) as client:
        resp = await client.post(_RESEND_URL, json=body, headers=req_headers)
    outcome = _classify_resend_response(resp, subject=subject)
    if outcome is None:
        raise EmailSendError(f"resend 5xx: {resp.status_code} {resp.text[:_LOG_BODY_TRUNCATE_5XX]}")
    return outcome


async def _post_with_retry(body: dict[str, Any], req_headers: dict[str, str], subject: str) -> bool:
    """Tenacity-wrapped POST. Returns the same bool as :func:`_post_resend`.

    On retry exhaustion the underlying exception propagates; the caller
    converts that into a ``False`` return + exception log.
    """
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(_RETRY_ATTEMPTS),
        wait=wait_exponential(min=_RETRY_WAIT_MIN, max=_RETRY_WAIT_MAX),
        retry=retry_if_exception_type((httpx.HTTPError, EmailSendError)),
        reraise=True,
    ):
        with attempt:
            return await _post_resend(body, req_headers, subject)
    return False


async def send_email(
    to: str | list[str],
    subject: str,
    html: str,
    reply_to: str | None = None,
    *,
    text: str | None = None,
    headers: dict[str, str] | None = None,
    attachments: list[Path] | None = None,
) -> bool:
    """POST a transactional email via Resend. Returns True on 2xx.

    Args:
        attachments: optional list of file paths. Each file is read,
            base64-encoded, and posted via the Resend ``attachments``
            field. Required for the LaTeX resume subsystem so the
            tailored / fallback PDF rides along with the cover letter.
            Per CLAUDE.md hard rule #5 the PDF is NEVER posted to a
            Discord channel — email attachment is the only delivery
            mechanism.
    """
    settings = get_settings()
    if not settings.resend_api_key or not settings.resend_from_email:
        _log.warning("resend_misconfigured")
        return False

    body = _build_resend_payload(
        from_email=settings.resend_from_email,
        to=to,
        subject=subject,
        html=html,
    )
    _apply_optional_fields(body, text=text, reply_to=reply_to, headers=headers)
    _attach_files(body, attachments)

    try:
        ok = await _post_with_retry(body, _auth_headers(settings.resend_api_key), subject)
    except Exception as e:
        _log.exception("email_send_failed", err=str(e), subject=subject)
        return False
    if ok:
        deliver_success_total.labels(channel="email").inc()
        _log.info("email_sent", to=body["to"], subject=subject)
    return ok
