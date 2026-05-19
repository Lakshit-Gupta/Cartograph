#!/usr/bin/env python3
"""Interactive sock-puppet identity loader.

Prompts via getpass (no echo on password line, no shell history of
credentials), encrypts via libsodium (master key from SOPS env), and
inserts a row into `identities`. Safer than the click CLI which would
expose passwords on the command line.

Run:
    sops exec-env secrets.yaml 'uv run python scripts/add_identity.py'

Then enter:
    platform     (e.g. internshala / cuvette / unstop / contra)
    account_label (e.g. friend-alice / friend-bob)
    username
    password     (hidden)
    email_alias  (optional; press enter to skip)
    cookies_json (optional; press enter to skip)

Re-running adds another row — one identity per (platform, account_label).
Listing existing rows: `mp identity status` from any worker container,
or `uv run python -m src.cli.main identity status` locally.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import sys
import warnings
from pathlib import Path

# Suppress getpass.GetPassWarning when stdin lacks a TTY (e.g. inside
# `docker compose exec` from a non-interactive parent). The password will
# echo, which is acceptable since the user runs this script themselves
# on a trusted dev box. No retry-with-fallback magic — one warning per
# run is the point of GetPassWarning, and we don't want it cluttering
# the prompt flow when the user already accepted the trade-off.
warnings.filterwarnings("ignore", category=getpass.GetPassWarning)

# Repo root on sys.path so `python scripts/...` works without setuptools install
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.db import close_pool, init_pool  # noqa: E402
from src.common.identity_vault import store  # noqa: E402


async def main() -> int:
    print("=== Cartograph identity vault ===")
    print("Encrypts credentials via libsodium and stores in `identities` table.")
    print()

    platform = input("platform (internshala/cuvette/unstop/contra/...): ").strip().lower()
    if not platform:
        print("platform required", file=sys.stderr)
        return 2

    account_label = input("account_label (e.g. friend-alice): ").strip()
    if not account_label:
        print("account_label required", file=sys.stderr)
        return 2

    username = input("username (email or handle): ").strip()
    if not username:
        print("username required", file=sys.stderr)
        return 2

    # When stdin is a real TTY, getpass hides the password. When it isn't
    # (docker compose exec without -i, sudo without -S, etc.), getpass
    # falls back to plain input() and the password echoes. We accept the
    # echo path here — the user runs this on their own box and asked for
    # the warning silenced.
    prompt = "password (hidden): " if sys.stdin.isatty() else "password (will echo): "
    password = getpass.getpass(prompt).strip()
    if not password:
        print("password required", file=sys.stderr)
        return 2

    email_alias = input("email_alias (optional, enter to skip): ").strip() or None

    cookies_raw = input("cookies_json (optional, paste JSON or enter to skip): ").strip()
    cookies = None
    if cookies_raw:
        try:
            cookies = json.loads(cookies_raw)
        except json.JSONDecodeError as e:
            print(f"cookies_json invalid: {e}", file=sys.stderr)
            return 2

    credentials = {"username": username, "password": password}

    await init_pool()
    try:
        ident_id = await store(
            user_id=1,
            platform=platform,
            account_label=account_label,
            credentials=credentials,
            cookies=cookies,
            email_alias=email_alias,
        )
    finally:
        await close_pool()

    print()
    print(f"stored: identity_id={ident_id}  platform={platform}  label={account_label}")
    print("Credentials encrypted at rest. master key in SOPS, never logged.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
