#!/usr/bin/env python3
"""Audit declared deps vs actual imports under src/.

Exits non-zero with a list of unused declared deps. Lint-only — does not catch
missing deps (let pip / uv handle that at install time).
"""
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

# Deps that are intentionally not imported in source — runtime CLI entrypoints,
# Postgres adapters used through the driver, or planned for next phase.
WHITELIST: set[str] = {
    "uvicorn",          # CLI runtime, no python import
    "pgvector",         # adapter used via asyncpg at the SQL boundary
    "scikit-learn",     # Phase-2 logistic regression on response rates
    "resend",           # Phase-2 SDK switch (currently raw httpx)
    "telethon",         # Day-13 telegram freelance scraper
}

# Map of pip name → root import module(s) used in code, when they differ.
PKG_TO_MODULE: dict[str, list[str]] = {
    "discord.py": ["discord"],
    "curl_cffi": ["curl_cffi"],
    "httpx": ["httpx"],
    "aiohttp": ["aiohttp"],
    "asyncpg": ["asyncpg"],
    "pgvector": ["pgvector"],
    "redis": ["redis"],
    "fastapi": ["fastapi"],
    "uvicorn": ["uvicorn"],
    "prometheus-client": ["prometheus_client"],
    "sentence-transformers": ["sentence_transformers"],
    "numpy": ["numpy"],
    "scikit-learn": ["sklearn"],
    "pydantic": ["pydantic"],
    "pydantic-settings": ["pydantic_settings"],
    "pyyaml": ["yaml"],
    "orjson": ["orjson"],
    "apscheduler": ["apscheduler"],
    "aioimaplib": ["aioimaplib"],
    "resend": ["resend"],
    "telethon": ["telethon"],
    "pynacl": ["nacl"],
    "cryptography": ["cryptography"],
    "click": ["click"],
    "rich": ["rich"],
    "beautifulsoup4": ["bs4"],
    "lxml": ["lxml"],
    "feedparser": ["feedparser"],
    "selectolax": ["selectolax"],
    "camoufox": ["camoufox"],
    "playwright": ["playwright"],
    "tenacity": ["tenacity"],
    "structlog": ["structlog"],
    "python-dateutil": ["dateutil"],
    "tzdata": [],  # data-only
}


def _deps_from_pyproject() -> list[str]:
    pyproj = tomllib.loads((ROOT / "pyproject.toml").read_text())
    deps = pyproj["project"]["dependencies"]
    out: list[str] = []
    for dep in deps:
        # Strip version + extras
        name = re.split(r"[<>=!\[~;]", dep, maxsplit=1)[0].strip()
        out.append(name)
    return out


def _all_imports() -> set[str]:
    pat = re.compile(r"^\s*(?:from\s+([A-Za-z_][\w.]*)|import\s+([A-Za-z_][\w.]*))", re.MULTILINE)
    seen: set[str] = set()
    for py in SRC.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="ignore")
        for m in pat.finditer(text):
            mod = (m.group(1) or m.group(2)).split(".")[0]
            seen.add(mod)
    return seen


def main() -> int:
    deps = _deps_from_pyproject()
    imports = _all_imports()
    unused: list[str] = []
    unknown: list[str] = []
    for dep in deps:
        if dep in WHITELIST:
            continue
        mods = PKG_TO_MODULE.get(dep)
        if mods is None:
            unknown.append(dep)
            continue
        if not mods:
            continue  # data-only deps
        if not any(m in imports for m in mods):
            unused.append(dep)
    if unused:
        print("UNUSED declared deps:")
        for d in unused:
            print(f"  - {d}")
    if unknown:
        print("UNKNOWN (no module mapping — add to PKG_TO_MODULE if real):")
        for d in unknown:
            print(f"  - {d}")
    if unused or unknown:
        return 1
    print("ok — all declared deps imported under src/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
