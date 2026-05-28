#!/usr/bin/env python3
"""Non-interactive identity loader — cookies + UA from a JSON file.

Sibling to the interactive `scripts/add_identity.py`. Use this when you've
exported cookies from a browser (e.g. via the Cookie-Editor extension) and
just want to drop them into the vault without typing username/password.

Cookies-only auth works for platforms that gate everything on session
cookies (Internshala, Cuvette, Unstop). For platforms that require a
password (e.g. forced re-auth after rotation) use the interactive
add_identity.py instead, or supply --username + --password here.

Usage:
    sops exec-env secrets.yaml 'uv run python scripts/add_identity_from_file.py \\
        --platform internshala \\
        --account-label raju \\
        --cookies-file /cookies/internshala/raju_internshala_cookies.json \\
        --ua "Mozilla/5.0 (X11; Linux x86_64; rv:151.0) Gecko/20100101 Firefox/151.0"'

Cookie file format: JSON array of cookie objects with at minimum `name` +
`value` (the Cookie-Editor export shape). The loader flattens it to
{name: value} pairs — Internshala's session cookie + CSRF token + a few
others is what matters; metadata like `httpOnly` / `secure` is dropped
because Playwright's add_cookies re-derives those from URL scope at
apply time (see src/workers/apply_browser_worker.py).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Repo root on sys.path so `python scripts/...` works without setuptools install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.db import close_pool, init_pool
from src.common.identity_vault import store


def _flatten_cookies(raw: object) -> dict[str, str]:
    """Accept either a Cookie-Editor JSON list OR a plain {name: value} dict.

    Cookie-Editor exports: [{"name": "x", "value": "y", "domain": "...", ...}, ...]
    Plain dict form:       {"x": "y", "a": "b"}

    Returns a flat {name: value} dict either way.
    """
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    if isinstance(raw, list):
        out: dict[str, str] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            value = item.get("value")
            if isinstance(name, str) and value is not None:
                out[name] = str(value)
        return out
    raise ValueError(f"unsupported cookies JSON shape: {type(raw).__name__}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Non-interactive identity loader")
    parser.add_argument("--platform", required=True, help="e.g. internshala / naukri / cuvette")
    parser.add_argument("--account-label", required=True, help="unique per platform, e.g. raju")
    parser.add_argument("--cookies-file", required=True, type=Path, help="path to JSON cookie file")
    parser.add_argument("--ua", required=True, help="User-Agent string captured from the same browser")
    parser.add_argument("--username", default="", help="optional — leave empty for cookie-only identities")
    parser.add_argument("--password", default="", help="optional — leave empty for cookie-only identities")
    parser.add_argument("--email-alias", default=None, help="optional email alias mapped to this account")
    args = parser.parse_args()

    if not args.cookies_file.exists():
        print(f"cookies file not found: {args.cookies_file}", file=sys.stderr)
        return 2

    try:
        raw = json.loads(args.cookies_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"cookies file is not valid JSON: {e}", file=sys.stderr)
        return 2

    try:
        cookies = _flatten_cookies(raw)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    if not cookies:
        print("no cookies extracted — file empty or wrong shape", file=sys.stderr)
        return 2

    credentials: dict[str, str] = {}
    if args.username:
        credentials["username"] = args.username
    if args.password:
        credentials["password"] = args.password
    # Even when no username/password, store() still encrypts the credentials
    # blob — keep a marker so downstream readers can tell intentional
    # cookie-only rows apart from corruption.
    if not credentials:
        credentials["mode"] = "cookies_only"

    await init_pool()
    try:
        ident_id = await store(
            user_id=1,
            platform=args.platform.lower(),
            account_label=args.account_label,
            credentials=credentials,
            cookies=cookies,
            email_alias=args.email_alias,
            ua_string=args.ua,
        )
    finally:
        await close_pool()

    print(
        f"stored: identity_id={ident_id}  platform={args.platform}  "
        f"label={args.account_label}  cookies={len(cookies)}  ua_len={len(args.ua)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
