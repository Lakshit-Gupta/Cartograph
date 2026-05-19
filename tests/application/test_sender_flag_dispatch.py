"""Hermetic tests for ``sender.send_application`` flag-driven dispatch.

Coverage:
  - ``is_latex_enabled() == False`` routes to legacy ``send_with_json_template``.
  - ``is_latex_enabled() == True`` routes to ``send_with_latex``.
  - LaTeX path raising drops to the legacy path (``except Exception:``).
  - The current tenant id is passed through to ``send_with_latex``.

DB + Resend + LLM are all mocked; the test exercises ``_dispatch_send_path``
only.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from src.application import sender as sender_mod

_OPP_ID = UUID("00000000-0000-0000-0000-00000000aaaa")

_OPP_ROW = {
    "id": _OPP_ID,
    "title": "Eng",
    "company": "Acme",
    "apply_method": "email",
    "apply_url": "mailto:a@b.c",
    "description": "",
}


@pytest.fixture
def _bypass_db(monkeypatch: pytest.MonkeyPatch):
    """Skip the real DB hops for opp fetch + the followups table init."""

    async def _fake_load(_id: UUID) -> dict[str, Any]:
        return dict(_OPP_ROW)

    async def _no_followups() -> None:
        return None

    monkeypatch.setattr(sender_mod, "_load_opp_or_raise", _fake_load)
    monkeypatch.setattr(sender_mod, "_ensure_followups_table", _no_followups)
    # Profile loaders read disk; stub with empty dicts.
    monkeypatch.setattr(sender_mod, "_load_profile_bundle", lambda: ({}, {"name": "Me"}, {}))
    yield


@pytest.mark.smoke
async def test_flag_off_routes_to_legacy(monkeypatch: pytest.MonkeyPatch, _bypass_db):
    monkeypatch.setattr(sender_mod, "is_latex_enabled", lambda: False)

    called: dict[str, Any] = {"legacy": 0, "latex": 0}

    async def _fake_legacy(*_a: Any, **_k: Any) -> dict[str, Any]:
        called["legacy"] += 1
        return {"application_id": 1, "method": "email", "via": "legacy"}

    async def _fake_latex(*_a: Any, **_k: Any) -> dict[str, Any]:
        called["latex"] += 1
        return {"application_id": 2, "via": "latex"}

    import src.application.sender_latex as latex_mod
    import src.application.sender_legacy as legacy_mod

    monkeypatch.setattr(legacy_mod, "send_with_json_template", _fake_legacy)
    monkeypatch.setattr(latex_mod, "send_with_latex", _fake_latex)

    out = await sender_mod.send_application(_OPP_ID)
    assert called["legacy"] == 1
    assert called["latex"] == 0
    assert out["via"] == "legacy"


@pytest.mark.smoke
async def test_flag_on_routes_to_latex(monkeypatch: pytest.MonkeyPatch, _bypass_db):
    monkeypatch.setattr(sender_mod, "is_latex_enabled", lambda: True)

    called: dict[str, Any] = {"legacy": 0, "latex": 0}

    async def _fake_legacy(*_a: Any, **_k: Any) -> dict[str, Any]:
        called["legacy"] += 1
        return {"via": "legacy"}

    async def _fake_latex(*_a: Any, **_k: Any) -> dict[str, Any]:
        called["latex"] += 1
        return {"via": "latex"}

    import src.application.sender_latex as latex_mod
    import src.application.sender_legacy as legacy_mod

    monkeypatch.setattr(legacy_mod, "send_with_json_template", _fake_legacy)
    monkeypatch.setattr(latex_mod, "send_with_latex", _fake_latex)

    out = await sender_mod.send_application(_OPP_ID)
    assert called["latex"] == 1
    assert called["legacy"] == 0
    assert out["via"] == "latex"


async def test_latex_path_failure_drops_to_legacy(monkeypatch: pytest.MonkeyPatch, _bypass_db):
    """A bug in the LaTeX path must NOT swallow the apply silently."""
    monkeypatch.setattr(sender_mod, "is_latex_enabled", lambda: True)

    async def _bad_latex(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise RuntimeError("parser crash on malformed manifest")

    legacy_called = {"v": 0}

    async def _fake_legacy(*_a: Any, **_k: Any) -> dict[str, Any]:
        legacy_called["v"] += 1
        return {"via": "legacy-fallback"}

    import src.application.sender_latex as latex_mod
    import src.application.sender_legacy as legacy_mod

    monkeypatch.setattr(latex_mod, "send_with_latex", _bad_latex)
    monkeypatch.setattr(legacy_mod, "send_with_json_template", _fake_legacy)

    out = await sender_mod.send_application(_OPP_ID)
    assert legacy_called["v"] == 1
    assert out["via"] == "legacy-fallback"


async def test_latex_path_receives_current_tenant(monkeypatch: pytest.MonkeyPatch, _bypass_db):
    """``send_with_latex`` is called with ``current_tenant()`` as user_id."""
    monkeypatch.setattr(sender_mod, "is_latex_enabled", lambda: True)

    captured: dict[str, Any] = {}

    async def _fake_latex(opp_id, opp, prof, summary, prefs, user_id, **kwargs) -> dict[str, Any]:
        captured["user_id"] = user_id
        captured["opp_id"] = opp_id
        return {"via": "latex"}

    import src.application.sender_latex as latex_mod
    from src.common import db as db_mod

    monkeypatch.setattr(latex_mod, "send_with_latex", _fake_latex)
    monkeypatch.setattr(db_mod, "current_tenant", lambda: 42)
    # sender imports current_tenant at module load time
    monkeypatch.setattr(sender_mod, "current_tenant", lambda: 42)

    await sender_mod.send_application(_OPP_ID)
    assert captured["user_id"] == 42
    assert captured["opp_id"] == _OPP_ID


def test_is_latex_enabled_reads_settings(monkeypatch: pytest.MonkeyPatch):
    """``is_latex_enabled`` is a pure read of the settings flag."""
    from src.common.secrets import get_settings

    monkeypatch.setenv("MP_RESUME_LATEX_ENABLED", "true")
    get_settings.cache_clear()
    try:
        assert sender_mod.is_latex_enabled() is True
    finally:
        get_settings.cache_clear()

    monkeypatch.setenv("MP_RESUME_LATEX_ENABLED", "false")
    get_settings.cache_clear()
    try:
        assert sender_mod.is_latex_enabled() is False
    finally:
        get_settings.cache_clear()


async def test_override_cover_markdown_passes_through(monkeypatch: pytest.MonkeyPatch, _bypass_db):
    """``override_cover_markdown`` reaches the LaTeX path verbatim."""
    monkeypatch.setattr(sender_mod, "is_latex_enabled", lambda: True)

    seen: dict[str, Any] = {}

    async def _fake_latex(*args: Any, override_cover_markdown=None, **kwargs: Any):
        seen["override"] = override_cover_markdown
        return {"via": "latex"}

    import src.application.sender_latex as latex_mod

    monkeypatch.setattr(latex_mod, "send_with_latex", _fake_latex)

    await sender_mod.send_application(_OPP_ID, override_cover_markdown="cover X")
    assert seen["override"] == "cover X"
