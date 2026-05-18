"""Tectonic-based LaTeX compile pipeline with sandbox + metadata scrub.

Hard rules (CLAUDE.md "LaTeX resume subsystem"):
- ``tectonic --untrusted`` ALWAYS. Disables ``\\write18``, restricts file
  reads to the working directory.
- Subprocess timeout 30 s; on timeout the process group is killed with
  ``SIGKILL`` so a stuck tectonic doesn't strand the worker.
- After successful compile, ``qpdf --linearize`` rewrites the PDF for
  faster streaming, then ``exiftool -all:all=`` strips every metadata
  tag (per hard rule #4 — PDF metadata must never reach the recipient).
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CompileResult:
    pdf_path: Path
    log_path: Path
    duration_ms: int
    tectonic_version: str


class CompileError(RuntimeError):
    """Tectonic / qpdf / exiftool failure surface for the apply fallback path."""


async def run(main_tex: Path, *, timeout: float = 30.0) -> CompileResult:
    """Compile ``main_tex`` to PDF using the sandboxed tectonic pipeline.

    Returns ``CompileResult`` on success. Raises ``CompileError`` on
    timeout, non-zero exit, or post-processing failure. Callers handle
    the exception by inserting a ``resume_compile_log`` row with
    ``status='failed'`` and falling back to the pre-warmed PDF from
    ``fallback.get_fallback(user_id)``.
    """
    t0 = time.perf_counter()
    cwd = main_tex.parent
    env = {
        **os.environ,
        "XDG_CACHE_HOME": os.environ.get("XDG_CACHE_HOME", "/var/lib/tectonic"),
    }

    # start_new_session=True puts tectonic in its own process group so
    # killpg can take it out cleanly on timeout.
    #
    # `--keep-intermediates` is a boolean flag (no value) in tectonic 0.16.x,
    # not a key=value option. Omitting it gets the default behaviour we
    # want: intermediates are discarded after compile, leaving only the PDF
    # and log next to the input. Passing `--keep-intermediates=false`
    # parses as the flag followed by a positional INPUT `=false`, which
    # tectonic then tries to compile and bails out on.
    proc = await asyncio.create_subprocess_exec(
        "tectonic",
        "-X",
        "compile",
        "--untrusted",
        str(main_tex),
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        await proc.wait()
        raise CompileError("tectonic timeout") from exc

    if proc.returncode != 0:
        msg = stderr.decode("utf-8", errors="replace")[:1000]
        raise CompileError(f"tectonic exit {proc.returncode}: {msg}")

    pdf = main_tex.with_suffix(".pdf")
    if not pdf.exists():
        raise CompileError("tectonic exited 0 but PDF missing")

    await _qpdf_linearize(pdf)
    await _exiftool_scrub(pdf)

    return CompileResult(
        pdf_path=pdf,
        log_path=main_tex.with_suffix(".log"),
        duration_ms=int((time.perf_counter() - t0) * 1000),
        tectonic_version=await _tectonic_version(),
    )


async def _qpdf_linearize(pdf: Path) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "qpdf", "--linearize", "--replace-input", str(pdf),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()
    except FileNotFoundError as exc:
        raise CompileError(f"qpdf missing: {exc}") from exc


async def _exiftool_scrub(pdf: Path) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "exiftool", "-all:all=", "-overwrite_original", str(pdf),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()
    except FileNotFoundError as exc:
        raise CompileError(f"exiftool missing: {exc}") from exc


async def _tectonic_version() -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "tectonic", "--version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        return out.decode("utf-8", errors="replace").strip().split("\n", 1)[0]
    except FileNotFoundError:
        return "tectonic: not installed"
