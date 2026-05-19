#!/usr/bin/env python3
"""One-shot Telegram MTProto auth.

Reads api_id / api_hash / session_name from SOPS-decrypted env, prompts
for phone number + SMS code (and 2FA password if set), and writes the
auth state to `<session_name>.session` next to this script. Copy that
.session file into the Pi at /var/lib/agent/telegram/ — the freelance
telegram-fetcher worker reuses it without re-auth.

Run:
    sops exec-env secrets.yaml 'uv run python scripts/telegram_auth.py'

Re-running after success is idempotent (session already authorised; exits 0
without prompting).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

try:
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError
except ImportError:
    sys.stderr.write("telethon not installed. Run: uv add telethon\n")
    sys.exit(1)


SESSION_DIR = Path(__file__).resolve().parents[1] / "var" / "telegram"


async def main() -> int:
    # SOPS-decrypted env keys are lowercase by project convention
    # (secrets.yaml keys map verbatim to env names). Don't uppercase.
    env = os.environ
    api_id_raw = env.get("telegram_api_id") or ""
    api_hash = env.get("telegram_api_hash") or ""
    session_name = env.get("telegram_session_name") or "Cartograph_freelance"

    if not api_id_raw or not api_hash:
        sys.stderr.write(
            "telegram_api_id / telegram_api_hash missing in env. "
            "Run via: sops exec-env secrets.yaml 'uv run python scripts/telegram_auth.py'\n"
        )
        return 2

    try:
        api_id = int(api_id_raw)
    except ValueError:
        sys.stderr.write(f"telegram_api_id is not numeric: {api_id_raw!r}\n")
        return 2

    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    session_path = SESSION_DIR / session_name
    print(f"session file -> {session_path}.session")

    client = TelegramClient(str(session_path), api_id, api_hash)
    await client.connect()
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"already authorised as {me.first_name} (@{me.username}) id={me.id}")
            return 0

        phone = input("phone number (incl. country code, e.g. +91XXXXXXXXXX): ").strip()
        if not phone.startswith("+"):
            sys.stderr.write("phone must start with + and country code\n")
            return 2

        sent = await client.send_code_request(phone)
        print(f"code sent via {sent.type.__class__.__name__}. check Telegram app or SMS.")
        code = input("enter 5-digit code: ").strip()

        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
        except SessionPasswordNeededError:
            pw = input("2FA password: ").strip()
            await client.sign_in(password=pw)

        me = await client.get_me()
        print(f"authorised as {me.first_name} (@{me.username}) id={me.id}")
        print(f"session written to {session_path}.session")
        print("\nNext: copy <session_path>.session into /var/lib/agent/telegram/ on the Pi")
        print("and mount it into the freelance-telegram-fetcher worker via compose.yaml.")
        return 0
    finally:
        await client.disconnect()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
