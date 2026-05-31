"""Config loading + URL building for the Internshala JOBS discovery worker.

Mirrors `internshala_discovery.config` (prefs > env > default resolution, the
RECON_PENDING boot guard, SIGHUP selector reload) but for the jobs vertical:
the listing pages are filtered via URL PATH (cities / work-from-home / salary /
fresher) rather than dropdowns, so there is no dropdown matrix — the "combos"
are the 1-2 URL variants `build_variants` produces.

`RECON_PENDING_SENTINEL` + `ReconPendingError` + the pure `load_selectors` yaml
reader are imported from the internship config (constants / pure helpers); the
boot guard is re-implemented here keyed on `INTERNSHALA_JOBS_ALLOW_RECON_PENDING`
so jobs and internship recon are gated independently.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from src.common.logger import get_logger
from src.workers.internshala_discovery.config import (
    RECON_PENDING_SENTINEL,
    ReconPendingError,
    load_selectors,
)

_log = get_logger(__name__)

# Hardcoded defaults.
_DEFAULT_IDLE_SEC = 180
_DEFAULT_BACKOFF_SEC = 600
_DEFAULT_SALARY_FLOOR_LPA = 12
_DEFAULT_MAX_EXPERIENCE_YEARS = 1
_DEFAULT_MAX_AGE_DAYS = 14
_DEFAULT_MAX_CYCLES_PER_ENGINE = 10
_DEFAULT_IDENTITY_LABEL = "raju_internshala"
_DEFAULT_PAGES_PER_URL = 3
_DEFAULT_MIN_SALARY_LPA_URL = 10  # Internshala's max URL salary option
_DEFAULT_CITIES = ["bangalore", "gurgaon", "pune", "uttar-pradesh", "ghaziabad"]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_SELECTORS_PATH = _REPO_ROOT / "config" / "sources" / "internshala_jobs_selectors.yaml"
_DEFAULT_KEYWORDS_PATH = _REPO_ROOT / "config" / "sources" / "internshala_jobs_keywords.yaml"
_NEGATIVE_KEYWORDS_PATH = _REPO_ROOT / "config" / "policy" / "negative_keywords.yaml"
_PREFS_PATH = _REPO_ROOT / "config" / "profile" / "prefs.yaml"

IDENTITY_PLATFORM = "internshala"  # shares the Internshala identity with internships
SOURCE_SLUG = "in_internshala_jobs"
_JOBS_BASE = "https://internshala.com"

__all__ = [
    "IDENTITY_PLATFORM",
    "RECON_PENDING_SENTINEL",
    "SOURCE_SLUG",
    "JobVariant",
    "JobsDiscoveryConfig",
    "ReconPendingError",
    "build_variants",
    "load_jobs_config",
    "reload_into",
]


@dataclass(frozen=True, slots=True)
class JobVariant:
    """One listing URL to crawl: `name` ∈ {general, fresher}, `url` the built path."""

    name: str
    url: str


def _slug_city(city: str) -> str:
    """`Uttar Pradesh` -> `uttar-pradesh`; idempotent on already-slugged input."""
    return city.strip().lower().replace(" ", "-")


def _build_url(prefix: str, cities: list[str], *, work_from_home: bool, min_salary_lpa_url: int) -> str:
    city_seg = ",".join(_slug_city(c) for c in cities)
    url = f"{_JOBS_BASE}/{prefix}/jobs-in-{city_seg}"
    if work_from_home:
        url += "/work-from-home"
    url += f"/salary-{min_salary_lpa_url}"
    return url


def build_variants(
    cities: list[str],
    *,
    work_from_home: bool,
    min_salary_lpa_url: int,
    crawl_fresher: bool,
    crawl_general: bool,
) -> list[JobVariant]:
    """Build the 1-2 listing URL variants from the URL-path filters.

    `crawl_general` -> the `/jobs/...` URL; `crawl_fresher` -> the
    `/fresher-jobs/...` URL. Both are crawled each cycle when both flags are set;
    Redis dedup collapses the overlap downstream.
    """
    variants: list[JobVariant] = []
    if crawl_general:
        variants.append(
            JobVariant("general", _build_url("jobs", cities, work_from_home=work_from_home, min_salary_lpa_url=min_salary_lpa_url))
        )
    if crawl_fresher:
        variants.append(
            JobVariant("fresher", _build_url("fresher-jobs", cities, work_from_home=work_from_home, min_salary_lpa_url=min_salary_lpa_url))
        )
    return variants


@dataclass(slots=True)
class JobsDiscoveryConfig:
    """Fully-resolved jobs worker config. `selectors` is swapped on SIGHUP."""

    idle_sec: int
    backoff_sec: int
    salary_floor_lpa: int
    max_experience_years: int
    max_age_days: int
    max_cycles_per_engine: int
    identity_label: str
    pages_per_url: int
    cities: list[str]
    min_salary_lpa_url: int
    work_from_home: bool
    crawl_fresher: bool
    crawl_general: bool
    selectors_path: Path
    selectors: dict[str, Any] = field(default_factory=dict)
    selectors_version: str = ""
    include_keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    once: bool = False
    dry_run: bool = False
    variant_filter: str | None = None

    @property
    def salary_floor_inr(self) -> float:
        """12 LPA -> 100000 INR/month. The strict comp_min floor."""
        return self.salary_floor_lpa * 100_000 / 12

    @property
    def listing_selectors(self) -> dict[str, str]:
        """The `selectors.listing` subtree — passed to the jobs card parser."""
        return dict(self.selectors.get("listing", {}))

    def active_variants(self) -> list[JobVariant]:
        """URL variants, filtered by `--variant` when set."""
        variants = build_variants(
            self.cities,
            work_from_home=self.work_from_home,
            min_salary_lpa_url=self.min_salary_lpa_url,
            crawl_fresher=self.crawl_fresher,
            crawl_general=self.crawl_general,
        )
        if self.variant_filter is None:
            return variants
        return [v for v in variants if v.name == self.variant_filter]


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        _log.warning("jobs_env_int_invalid", var=name, value=raw, fallback=default)
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data or {}


def _prefs_overrides() -> dict[str, Any]:
    """`prefs.yaml -> discovery.internshala_jobs` block, or {} when absent."""
    if not _PREFS_PATH.exists():
        return {}
    try:
        prefs = _read_yaml(_PREFS_PATH)
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning("jobs_prefs_read_failed", path=str(_PREFS_PATH), err=str(exc))
        return {}
    discovery = prefs.get("discovery") or {}
    block = discovery.get("internshala_jobs") or {}
    return dict(block) if isinstance(block, dict) else {}


def _load_keywords() -> tuple[list[str], list[str]]:
    """`(include_title_any, exclude)` for the field-relevance gate.

    Include comes from `internshala_jobs_keywords.yaml:include_title_any` (the
    positive field filter — title must match one). Exclude is the union of that
    file's `exclude_any` plus the shared `negative_keywords.yaml` (every group
    except `borderline_disabled`), so jobs reuse the same hard-reject list as the
    internship auto-apply path. Missing files degrade to empty (no filtering).
    """
    include: list[str] = []
    exclude: list[str] = []

    kw_path = Path(os.environ.get("INTERNSHALA_JOBS_KEYWORDS_PATH") or _DEFAULT_KEYWORDS_PATH)
    if kw_path.exists():
        try:
            doc = _read_yaml(kw_path)
            include = [str(t).strip().lower() for t in (doc.get("include_title_any") or []) if str(t).strip()]
            exclude += [str(t).strip().lower() for t in (doc.get("exclude_any") or []) if str(t).strip()]
        except Exception as exc:  # pragma: no cover - defensive
            _log.warning("jobs_keywords_read_failed", path=str(kw_path), err=str(exc))

    if _NEGATIVE_KEYWORDS_PATH.exists():
        try:
            block = _read_yaml(_NEGATIVE_KEYWORDS_PATH).get("negative_keywords") or {}
            for group, terms in block.items():
                if group == "borderline_disabled" or not isinstance(terms, list):
                    continue
                exclude += [str(t).strip().lower() for t in terms if str(t).strip()]
        except Exception as exc:  # pragma: no cover - defensive
            _log.warning("jobs_negative_keywords_read_failed", err=str(exc))

    return sorted(set(include)), sorted(set(exclude))


def _guard_recon_pending(version: str) -> None:
    """Refuse to boot on RECON_PENDING unless INTERNSHALA_JOBS_ALLOW_RECON_PENDING=1."""
    if version != RECON_PENDING_SENTINEL:
        return
    if os.environ.get("INTERNSHALA_JOBS_ALLOW_RECON_PENDING") == "1":
        _log.warning("jobs_selectors_recon_pending_allowed", detail="RECON_PENDING allowed (dry-run/smoke only)")
        return
    raise ReconPendingError(
        "internshala_jobs_selectors.yaml still has version=RECON_PENDING. "
        "Capture live jobs selectors on the ThinkPad and bump the version before deploy, "
        "or set INTERNSHALA_JOBS_ALLOW_RECON_PENDING=1 for a --dry-run smoke."
    )


def load_jobs_config(
    *,
    once: bool = False,
    dry_run: bool = False,
    variant_filter: str | None = None,
) -> JobsDiscoveryConfig:
    """Build the resolved `JobsDiscoveryConfig` from env + prefs + the selectors YAML."""
    prefs = _prefs_overrides()

    def _int(key: str, env: str, default: int) -> int:
        env_val = _env_int(env, default)
        if key in prefs and prefs[key] is not None:
            try:
                return int(prefs[key])
            except (TypeError, ValueError):
                _log.warning("jobs_prefs_int_invalid", key=key, value=prefs[key], fallback=env_val)
        return env_val

    def _bool(key: str, env: str, default: bool) -> bool:
        env_val = _env_bool(env, default)
        if key in prefs and prefs[key] is not None:
            return bool(prefs[key])
        return env_val

    def _cities() -> list[str]:
        if isinstance(prefs.get("cities"), list) and prefs["cities"]:
            return [str(c) for c in prefs["cities"]]
        env_raw = os.environ.get("INTERNSHALA_JOBS_CITIES")
        if env_raw:
            return [c.strip() for c in env_raw.split(",") if c.strip()]
        return list(_DEFAULT_CITIES)

    selectors_path = Path(os.environ.get("INTERNSHALA_JOBS_SELECTORS_PATH") or _DEFAULT_SELECTORS_PATH)
    selectors, selectors_version = load_selectors(selectors_path)
    _guard_recon_pending(selectors_version)

    include_keywords, exclude_keywords = _load_keywords()

    identity_label = (
        str(prefs["identity_label"])
        if prefs.get("identity_label")
        else os.environ.get("INTERNSHALA_JOBS_IDENTITY_LABEL", _DEFAULT_IDENTITY_LABEL)
    )

    cfg = JobsDiscoveryConfig(
        idle_sec=_int("idle_sec", "INTERNSHALA_JOBS_IDLE_SEC", _DEFAULT_IDLE_SEC),
        backoff_sec=_int("backoff_sec", "INTERNSHALA_JOBS_BACKOFF_SEC", _DEFAULT_BACKOFF_SEC),
        salary_floor_lpa=_int("salary_floor_lpa", "INTERNSHALA_JOBS_SALARY_FLOOR_LPA", _DEFAULT_SALARY_FLOOR_LPA),
        max_experience_years=_int("max_experience_years", "INTERNSHALA_JOBS_MAX_EXPERIENCE_YEARS", _DEFAULT_MAX_EXPERIENCE_YEARS),
        max_age_days=_int("max_age_days", "INTERNSHALA_JOBS_MAX_AGE_DAYS", _DEFAULT_MAX_AGE_DAYS),
        max_cycles_per_engine=_int("max_cycles_per_engine", "INTERNSHALA_JOBS_MAX_CYCLES_PER_ENGINE", _DEFAULT_MAX_CYCLES_PER_ENGINE),
        identity_label=identity_label,
        pages_per_url=_int("pages_per_url", "INTERNSHALA_JOBS_PAGES_PER_URL", _DEFAULT_PAGES_PER_URL),
        cities=_cities(),
        min_salary_lpa_url=_int("min_salary_lpa_url", "INTERNSHALA_JOBS_MIN_SALARY_LPA_URL", _DEFAULT_MIN_SALARY_LPA_URL),
        work_from_home=_bool("work_from_home", "INTERNSHALA_JOBS_WORK_FROM_HOME", True),
        crawl_fresher=_bool("crawl_fresher", "INTERNSHALA_JOBS_CRAWL_FRESHER", True),
        crawl_general=_bool("crawl_general", "INTERNSHALA_JOBS_CRAWL_GENERAL", True),
        selectors_path=selectors_path,
        selectors=selectors,
        selectors_version=selectors_version,
        include_keywords=include_keywords,
        exclude_keywords=exclude_keywords,
        once=once,
        dry_run=dry_run,
        variant_filter=variant_filter,
    )
    _log.info(
        "jobs_config_loaded",
        salary_floor_lpa=cfg.salary_floor_lpa,
        max_experience_years=cfg.max_experience_years,
        cities=len(cfg.cities),
        variants=len(cfg.active_variants()),
        pages_per_url=cfg.pages_per_url,
        include_keywords=len(cfg.include_keywords),
        exclude_keywords=len(cfg.exclude_keywords),
        selectors_version=cfg.selectors_version,
        dry_run=cfg.dry_run,
        once=cfg.once,
    )
    return cfg


def reload_into(cfg: JobsDiscoveryConfig) -> None:
    """Re-read the selectors + keyword YAMLs and swap them in place (SIGHUP). A
    bad YAML keeps the old values + logs `jobs_selectors_reload_failed` — never
    takes the worker offline."""
    try:
        selectors, selectors_version = load_selectors(cfg.selectors_path)
    except Exception as exc:
        _log.warning("jobs_selectors_reload_failed", err=str(exc))
        return
    old = cfg.selectors_version
    cfg.selectors = selectors
    cfg.selectors_version = selectors_version
    cfg.include_keywords, cfg.exclude_keywords = _load_keywords()
    _log.info(
        "jobs_selectors_reloaded",
        old=old,
        new=selectors_version,
        include_keywords=len(cfg.include_keywords),
        exclude_keywords=len(cfg.exclude_keywords),
    )
