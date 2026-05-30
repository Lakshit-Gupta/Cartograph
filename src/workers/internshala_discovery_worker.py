"""Internshala browser-discovery worker — ThinkPad-resident entrypoint.

Drives Internshala's dropdown UI through a `BrowserEngine` (camoufox), scrapes
listing cards, post-filters to >= `INTERNSHALA_COMP_FLOOR_INR` (default
₹30,000/month), dedups against Redis, and persists survivors via
`persist_and_publish`. Loop-with-sleep (NO cron); one consumer per Internshala
identity. Selectors hot-reload on SIGHUP. Heartbeat to Redis every 30 s. One
cycle-report per cycle to `discovery_cycle_log` + `stream:notify`.

Runs on the spare Pop OS desktop only — the Pi never browses Internshala. The
worker reaches the Pi's Redis + Postgres over the autossh tunnel terminating on
the spare's loopback (see docs/runbooks/sidecar_setup.md).

Helper modules live under `src/workers/internshala_discovery/` (config, report,
cycle) to keep each file under the repo's 300-line ceiling.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import time
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import uuid4

from src.common.db import close_pool, init_pool
from src.common.logger import configure_logging, get_logger
from src.common.metrics import (
    discovery_cycles_total,
    discovery_heartbeat_timestamp,
)
from src.common.queue import RedisQ
from src.fetchers.browser.camoufox_engine import CamoufoxEngine
from src.fetchers.browser.engine import BrowserEngine
from src.workers.internshala_discovery.config import (
    IDENTITY_PLATFORM,
    DiscoveryConfig,
    load_config,
    reload_into,
)
from src.workers.internshala_discovery.cycle import run_cycle
from src.workers.internshala_discovery.persistence import (
    emit_cycle_failure_alert,
    persist_cycle_report,
    resolve_source_id,
)

configure_logging("internshala_discovery_worker")
_log = get_logger(__name__)

_HEARTBEAT_KEY = "discovery:heartbeat"
_HEARTBEAT_INTERVAL_SEC = 30
_HEARTBEAT_TTL_SEC = 90
_INTERNSHALA_BASE = "https://internshala.com"
_MAX_BACKOFF_SEC = 1_800


def _cookies_to_playwright(cookies: dict[str, str], base: str = _INTERNSHALA_BASE) -> list[dict]:
    """Convert the identity dict (name->value) to the Playwright cookie-list
    shape. Mirrors `src/fetchers/browser/camoufox.py:_cookies_to_playwright` —
    inlined so the worker does not import the fetcher tier."""
    parsed = urlparse(base)
    host = parsed.hostname or ""
    url = f"{parsed.scheme}://{host}" if host else base
    return [{"name": k, "value": v, "url": url} for k, v in cookies.items()]


async def _heartbeat_loop(q: RedisQ, stop: asyncio.Event) -> None:
    """`SET discovery:heartbeat <ts> EX 90` every 30 s until stop is set."""
    while not stop.is_set():
        ts = time.time()
        try:
            await q.raw.set(_HEARTBEAT_KEY, str(ts), ex=_HEARTBEAT_TTL_SEC)
            discovery_heartbeat_timestamp.set(ts)
        except Exception as exc:
            _log.warning("discovery_heartbeat_failed", err=str(exc))
        try:
            await asyncio.wait_for(stop.wait(), timeout=_HEARTBEAT_INTERVAL_SEC)
        except TimeoutError:
            continue


def _register_signals(stop: asyncio.Event, cfg: DiscoveryConfig) -> None:
    """SIGINT/SIGTERM -> graceful stop; SIGHUP -> reload both YAMLs in place."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        loop.add_signal_handler(signal.SIGHUP, lambda: reload_into(cfg))
    except (NotImplementedError, AttributeError):  # pragma: no cover - non-POSIX
        _log.warning("discovery_sighup_unsupported")


async def _run_loop(
    q: RedisQ,
    cfg: DiscoveryConfig,
    engine: BrowserEngine,
    cookies: list[dict],
    ua: str | None,
    source_id: int,
    worker_id: str,
    stop: asyncio.Event,
) -> None:
    """The loop-with-sleep heart. Exponential backoff on whole-cycle failure;
    `engine.restart()` every `max_cycles_per_engine` cycles."""
    cycle_index = 0
    error_streak = 0
    while not stop.is_set():
        t0 = time.monotonic()
        started_at = datetime.now(UTC).isoformat()
        cycle_id = str(uuid4())
        try:
            report = await run_cycle(
                engine,
                cookies,
                ua,
                q,
                cfg,
                worker_id=worker_id,
                cycle_id=cycle_id,
                started_at=started_at,
                source_id=source_id,
            )
            error_streak = 0
            discovery_cycles_total.labels(healthy=str(report.healthy).lower()).inc()
            await persist_cycle_report(q, report)
            idle = max(cfg.idle_sec - (time.monotonic() - t0), 0.0)
        except Exception as exc:
            error_streak += 1
            await emit_cycle_failure_alert(q, exc, error_streak=error_streak)
            idle = min(cfg.backoff_sec * (2 ** (error_streak - 1)), _MAX_BACKOFF_SEC)

        if cfg.once:
            break

        await _sleep_or_stop(stop, idle)
        cycle_index += 1
        if cycle_index % cfg.max_cycles_per_engine == 0:
            _log.info("discovery_engine_restart", cycle_index=cycle_index)
            try:
                await engine.restart()
            except Exception as exc:
                _log.warning("discovery_engine_restart_failed", err=str(exc))


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    """Sleep up to `seconds`, returning early if stop fires."""
    if seconds <= 0:
        return
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        return


async def serve(cfg: DiscoveryConfig) -> int:
    """Bootstrap (DB, Redis, identity, engine, heartbeat) then run the loop.

    Shared by the long-running worker `main()` and the `mp internshala-discover`
    CLI; the CLI builds `cfg` with `once`/`dry_run`/`combo_filter` flags and the
    same bootstrap runs exactly one cycle when `cfg.once` is set.
    """
    worker_id = f"discovery-{socket.gethostname()}-{os.getpid()}"

    await init_pool()
    q = await RedisQ.connect()

    stop = asyncio.Event()
    _register_signals(stop, cfg)

    source_id = await resolve_source_id()

    from src.common import identity_vault

    lease = await identity_vault.checkout(
        platform=IDENTITY_PLATFORM,
        worker_id=worker_id,
        lease_seconds=max(cfg.idle_sec * (cfg.max_cycles_per_engine + 2), 3_600),
    )
    if lease is None:
        _log.error("discovery_no_identity", platform=IDENTITY_PLATFORM)
        await close_pool()
        return 2

    cookies = _cookies_to_playwright(lease.cookies)
    engine: BrowserEngine = CamoufoxEngine(headless=True, restart_after_cycles=cfg.max_cycles_per_engine)
    heartbeat = asyncio.create_task(_heartbeat_loop(q, stop))

    _log.info("discovery_worker_ready", worker_id=worker_id, source_id=source_id, combos=len(cfg.matrix))
    exit_code = 0
    try:
        await _run_loop(q, cfg, engine, cookies, lease.ua_string, source_id, worker_id, stop)
    except Exception as exc:  # pragma: no cover - top-level safety net
        _log.exception("discovery_worker_fatal", err=str(exc))
        exit_code = 1
    finally:
        stop.set()
        heartbeat.cancel()
        try:
            await heartbeat
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await engine.shutdown()
        except Exception as exc:
            _log.warning("discovery_engine_shutdown_failed", err=str(exc))
        try:
            await identity_vault.release(lease.lease_id)
        except Exception as exc:
            _log.warning("discovery_identity_release_failed", err=str(exc))
        await close_pool()
    return exit_code


async def main() -> int:
    """Container entrypoint — long-running worker. `--once` is for the CLI."""
    cfg = load_config(once=os.environ.get("INTERNSHALA_ONCE") == "1")
    return await serve(cfg)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
