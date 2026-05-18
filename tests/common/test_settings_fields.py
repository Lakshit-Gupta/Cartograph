"""Settings model shape contract — fails loudly if a Gmail field is missing.

This is a model-shape test, NOT a configured-correctly test. Empty string
values still pass — the assertion is on the attribute existing with the
right type. Catches the failure class where a SOPS env key has been added
but the matching `Settings` field declaration was forgotten, causing
pydantic-settings to silently drop the env var on the floor.
"""

from __future__ import annotations

import pytest

from src.common.secrets import Settings, get_settings

REQUIRED_GMAIL_FIELDS: tuple[str, ...] = (
    "gmail_user",
    "gmail_oauth_client_id",
    "gmail_oauth_refresh_token",
    "gmail_worker_app_password",
)


@pytest.mark.smoke
def test_settings_has_gmail_fields() -> None:
    """Every Gmail field declared on Settings must be a `str` attribute.

    Empty string is OK — this contract is about presence + type, not value.
    """
    settings = get_settings()
    for name in REQUIRED_GMAIL_FIELDS:
        assert hasattr(settings, name), (
            f"Settings model missing required field: {name!r}. "
            "If you added a new SOPS env key, declare the matching Pydantic "
            "field on Settings in src/common/secrets.py."
        )
        value = getattr(settings, name)
        assert isinstance(value, str), f"Settings field {name!r} must be `str`, got {type(value).__name__}"


@pytest.mark.smoke
def test_settings_class_declares_gmail_fields() -> None:
    """Class-level declaration check — survives `get_settings` cache quirks.

    Pydantic v2 exposes declared fields via `model_fields`. This guards
    against runtime-only patching that wouldn't catch the bug at boot.
    """
    declared = Settings.model_fields
    for name in REQUIRED_GMAIL_FIELDS:
        assert name in declared, (
            f'Settings model has no field declaration for {name!r}; add `{{name}}: str = ""` to src/common/secrets.py::Settings.'
        )
        field_info = declared[name]
        assert field_info.annotation is str, f"Settings field {name!r} annotated as {field_info.annotation}, expected `str`"
