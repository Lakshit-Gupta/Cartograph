"""`mp identity ...` — status / add (encrypts via libsodium vault)."""

from __future__ import annotations

import asyncio
import json

import click

from src.common.db import acquire, close_pool, init_pool
from src.common.identity_vault import generate_master_key_hex, store


@click.group("identity")
def identity_group() -> None:
    """Manage platform identities."""


@identity_group.command("status")
def status() -> None:
    asyncio.run(_status())


async def _status() -> None:
    await init_pool()
    async with acquire() as conn:
        rows = await conn.fetch(
            "SELECT platform, account_label, ban_status, warmup_score, warmup_completed, last_used_at FROM identities ORDER BY platform"
        )
    for r in rows:
        click.echo(
            f"{r['platform']:14s} {r['account_label']:30s} "
            f"{r['ban_status']:12s} warmup={r['warmup_score']:.2f} "
            f"completed={r['warmup_completed']} last={r['last_used_at']}"
        )
    await close_pool()


@identity_group.command("add")
@click.option("--platform", required=True)
@click.option("--label", required=True)
@click.option("--credentials-json", required=True, help='e.g. {"username":"...","password":"..."}')
@click.option("--cookies-json", default="{}", help="optional cookies dict")
@click.option("--email-alias", default=None)
def add(platform: str, label: str, credentials_json: str, cookies_json: str, email_alias: str | None) -> None:
    asyncio.run(_add(platform, label, credentials_json, cookies_json, email_alias))


async def _add(platform: str, label: str, credentials_json: str, cookies_json: str, email_alias: str | None) -> None:
    creds = json.loads(credentials_json)
    cookies = json.loads(cookies_json) if cookies_json else None
    await init_pool()
    ident_id = await store(
        user_id=1,
        platform=platform,
        account_label=label,
        credentials=creds,
        cookies=cookies,
        email_alias=email_alias,
    )
    await close_pool()
    click.echo(f"identity stored: id={ident_id}")


@identity_group.command("gen-master-key")
def gen_master_key() -> None:
    click.echo(generate_master_key_hex())
