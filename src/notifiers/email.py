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

_RESEND_URL = "https://api.resend.com/emails"


class EmailSendError(RuntimeError):
    pass


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

    body: dict[str, Any] = {
        "from": settings.resend_from_email,
        "to": [to] if isinstance(to, str) else list(to),
        "subject": subject,
        "html": html,
    }
    if text is not None:
        body["text"] = text
    if reply_to:
        body["reply_to"] = reply_to
    if headers:
        body["headers"] = headers
    if attachments:
        body["attachments"] = []
        for att in attachments:
            try:
                data = att.read_bytes()
            except OSError as e:
                _log.warning("attachment_read_failed", path=str(att), err=str(e))
                continue
            body["attachments"].append(
                {
                    "filename": att.name,
                    "content": base64.b64encode(data).decode("ascii"),
                }
            )
        if not body["attachments"]:
            # All attachments unreadable — drop the field so Resend doesn't
            # reject the message on the empty array.
            del body["attachments"]

    req_headers = {
        "Authorization": f"Bearer {settings.resend_api_key}",
        "Content-Type": "application/json",
    }

    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(min=1, max=10),
            retry=retry_if_exception_type((httpx.HTTPError, EmailSendError)),
            reraise=True,
        ):
            with attempt:
                async with httpx.AsyncClient(timeout=20.0) as client:
                    resp = await client.post(_RESEND_URL, json=body, headers=req_headers)
                    if resp.status_code >= 500:
                        raise EmailSendError(f"resend 5xx: {resp.status_code} {resp.text[:200]}")
                    if resp.status_code >= 400:
                        # 4xx is non-retryable.
                        _log.error(
                            "resend_4xx",
                            status=resp.status_code,
                            body=resp.text[:300],
                            subject=subject,
                        )
                        return False
                    deliver_success_total.labels(channel="email").inc()
                    _log.info("email_sent", to=body["to"], subject=subject)
                    return True
    except Exception as e:
        _log.exception("email_send_failed", err=str(e), subject=subject)
        return False
    return False
