"""Identity vault — per-row libsodium crypto_secretbox over BYTEA columns.

Master key lives in SOPS-encrypted secrets.yaml (LIBSODIUM_MASTER_KEY_HEX),
delivered via env at compose-up. NEVER log decrypted contents.
"""
from __future__ import annotations

import json
import secrets as _stdlib_secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import nacl.secret
import nacl.utils

from src.common.db import acquire
from src.common.logger import get_logger
from src.common.secrets import get_settings
from src.common.types import IdentityLease

_log = get_logger(__name__)


@dataclass(slots=True)
class _DecryptedIdentity:
    identity_id: int
    platform: str
    account_label: str
    credentials: dict[str, Any]
    cookies: dict[str, str]
    ua_string: str | None


def _box() -> nacl.secret.SecretBox:
    return nacl.secret.SecretBox(get_settings().libsodium_master_key)


def encrypt(plaintext: dict[str, Any]) -> tuple[bytes, bytes]:
    """Return (ciphertext, nonce)."""
    nonce = nacl.utils.random(nacl.secret.SecretBox.NONCE_SIZE)
    raw = json.dumps(plaintext, separators=(",", ":")).encode()
    enc = _box().encrypt(raw, nonce=nonce)
    # nacl.encrypt returns nonce+ciphertext; store ciphertext separately to allow rotation
    return enc.ciphertext, nonce


def decrypt(ciphertext: bytes, nonce: bytes) -> dict[str, Any]:
    raw = _box().decrypt(ciphertext, nonce=nonce)
    return json.loads(raw.decode())


async def store(
    *,
    user_id: int,
    platform: str,
    account_label: str,
    credentials: dict[str, Any],
    cookies: dict[str, str] | None = None,
    ua_string: str | None = None,
    fingerprint_id: int | None = None,
    email_alias: str | None = None,
) -> int:
    # Embed ua_string inside encrypted credentials so it travels through one box
    creds_with_ua = dict(credentials)
    if ua_string is not None:
        creds_with_ua["ua_string"] = ua_string
    cred_ct, cred_nonce = encrypt(creds_with_ua)
    cookie_ct, cookie_nonce = (b"", b"")
    if cookies:
        cookie_ct, cookie_nonce = encrypt(cookies)

    async with acquire() as conn:
        rec = await conn.fetchrow(
            """
            INSERT INTO identities (
                user_id, platform, account_label,
                encrypted_credentials, cred_nonce,
                encrypted_cookies, cookie_nonce,
                fingerprint_id, email_alias
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            ON CONFLICT (platform, account_label) DO UPDATE SET
                encrypted_credentials = EXCLUDED.encrypted_credentials,
                cred_nonce            = EXCLUDED.cred_nonce,
                encrypted_cookies     = EXCLUDED.encrypted_cookies,
                cookie_nonce          = EXCLUDED.cookie_nonce,
                fingerprint_id        = COALESCE(EXCLUDED.fingerprint_id, identities.fingerprint_id),
                email_alias           = COALESCE(EXCLUDED.email_alias, identities.email_alias)
            RETURNING id
            """,
            user_id, platform, account_label,
            cred_ct, cred_nonce, cookie_ct, cookie_nonce,
            fingerprint_id, email_alias,
        )
        ident_id = int(rec["id"])
        await conn.execute(
            "INSERT INTO user_identities(user_id, identity_id, role) VALUES ($1,$2,'owner') "
            "ON CONFLICT DO NOTHING",
            user_id, ident_id,
        )
        await conn.execute(
            "INSERT INTO identity_audit(identity_id, action, actor) VALUES ($1,'store','system')",
            ident_id,
        )
    _log.info("identity_stored", platform=platform, label=account_label)  # never log creds
    return ident_id


async def _load(identity_id: int) -> _DecryptedIdentity:
    async with acquire() as conn:
        rec = await conn.fetchrow(
            """
            SELECT id, platform, account_label,
                   encrypted_credentials, cred_nonce,
                   encrypted_cookies, cookie_nonce
            FROM identities WHERE id = $1
            """,
            identity_id,
        )
    if rec is None:
        raise KeyError(f"identity {identity_id} not found")
    creds = decrypt(bytes(rec["encrypted_credentials"]), bytes(rec["cred_nonce"])) if rec["encrypted_credentials"] else {}
    cookies = {}
    if rec["encrypted_cookies"]:
        cookies = decrypt(bytes(rec["encrypted_cookies"]), bytes(rec["cookie_nonce"]))
    ua = creds.get("ua_string")
    return _DecryptedIdentity(
        identity_id=int(rec["id"]),
        platform=rec["platform"],
        account_label=rec["account_label"],
        credentials=creds,
        cookies=cookies,
        ua_string=ua,
    )


async def checkout(
    *,
    platform: str,
    worker_id: str,
    lease_seconds: int = 600,
) -> IdentityLease | None:
    """Atomically lease a healthy identity for `worker_id`."""
    expires = datetime.now(UTC) + timedelta(seconds=lease_seconds)
    async with acquire() as conn, conn.transaction():
        rec = await conn.fetchrow(
            """
                WITH leased AS (
                    SELECT i.id
                    FROM identities i
                    WHERE i.platform = $1
                      AND i.ban_status = 'healthy'
                      AND NOT EXISTS (
                          SELECT 1 FROM identity_checkouts c
                          WHERE c.identity_id = i.id AND c.returned_at IS NULL
                            AND c.expires_at > NOW()
                      )
                    ORDER BY i.last_used_at NULLS FIRST
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                INSERT INTO identity_checkouts(identity_id, worker_id, expires_at)
                SELECT id, $2, $3 FROM leased
                RETURNING id, identity_id
                """,
            platform, worker_id, expires,
        )
        if rec is None:
            return None
        decrypted = await _load(int(rec["identity_id"]))
        await conn.execute(
            "UPDATE identities SET last_used_at = NOW() WHERE id = $1",
            decrypted.identity_id,
        )
    return IdentityLease(
        identity_id=decrypted.identity_id,
        platform=decrypted.platform,
        cookies=decrypted.cookies,
        ua_string=decrypted.ua_string,
        lease_id=int(rec["id"]),
        expires_at=expires,
    )


async def release(lease_id: int) -> None:
    async with acquire() as conn:
        await conn.execute(
            "UPDATE identity_checkouts SET returned_at = NOW() WHERE id = $1 AND returned_at IS NULL",
            lease_id,
        )


async def mark_banned(identity_id: int, reason: str) -> None:
    async with acquire() as conn:
        await conn.execute(
            "UPDATE identities SET ban_status = 'banned' WHERE id = $1",
            identity_id,
        )
        await conn.execute(
            "INSERT INTO identity_audit(identity_id, action, actor, metadata) VALUES ($1,'ban','system',$2::jsonb)",
            identity_id, json.dumps({"reason": reason}),
        )
    _log.warning("identity_banned", identity_id=identity_id, reason=reason)


def generate_master_key_hex() -> str:
    return _stdlib_secrets.token_hex(nacl.secret.SecretBox.KEY_SIZE)
