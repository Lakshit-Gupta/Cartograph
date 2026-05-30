"""Config loading for the Internshala discovery worker.

Three sources, lowest-to-highest precedence:
  1. Hardcoded defaults (the spec's Appendix B values).
  2. Environment variables (`INTERNSHALA_*`).
  3. `config/profile/prefs.yaml` -> `discovery.internshala.*` (when present).

`prefs.yaml` overrides env for the three tunables that live in both places
(`idle_sec`, `comp_floor_inr`, `max_cycles_per_engine`) so the operator can
change cadence by editing one checked-in file without touching `.env.sidecar`.

The two selector / matrix YAMLs are re-read on SIGHUP; everything else is read
once at boot. `load_config()` enforces the RECON-first invariant: it refuses to
return while `selectors.version == "RECON_PENDING"` unless
`INTERNSHALA_ALLOW_RECON_PENDING=1` is set (so `--dry-run` smoke can still run).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from src.common.logger import get_logger

_log = get_logger(__name__)

RECON_PENDING_SENTINEL = "RECON_PENDING"

# Hardcoded defaults — mirror the spec Appendix B table.
_DEFAULT_IDLE_SEC = 180
_DEFAULT_BACKOFF_SEC = 600
_DEFAULT_COMP_FLOOR_INR = 30_000
_DEFAULT_MAX_CYCLES_PER_ENGINE = 10
_DEFAULT_IDENTITY_LABEL = "raju_internshala"
_DEFAULT_PAGES_PER_URL = 3

_REPO_ROOT = Path(__file__).resolve().parents[3]  # config.py -> repo root
_DEFAULT_SELECTORS_PATH = _REPO_ROOT / "config" / "sources" / "internshala_selectors.yaml"
_DEFAULT_MATRIX_PATH = _REPO_ROOT / "config" / "sources" / "internshala_dropdown_matrix.yaml"
_PREFS_PATH = _REPO_ROOT / "config" / "profile" / "prefs.yaml"

# identity_vault.checkout() platform key — discovery + apply share one row (Phase 1).
IDENTITY_PLATFORM = "internshala"
SOURCE_SLUG = "in_internshala"
INTERNSHALA_LISTING_URL = "https://internshala.com/internships/"


class ReconPendingError(SystemExit):
    """Raised (a SystemExit, so the process exits non-zero) when the selector YAML
    still carries the RECON_PENDING placeholder and INTERNSHALA_ALLOW_RECON_PENDING=1
    is unset."""


@dataclass(frozen=True, slots=True)
class Combo:
    """One dropdown click sequence: category + work mode."""

    category: str
    work_mode: str

    @property
    def name(self) -> str:
        """Stable, filesystem-safe combo identifier used in logs, metrics,
        screenshot filenames, and the `--combo` CLI filter."""
        slug = self.category.lower()
        for ch in (" ", "/", "(", ")", ".", ",", "&"):
            slug = slug.replace(ch, "-")
        while "--" in slug:
            slug = slug.replace("--", "-")
        return f"{slug.strip('-')}-{self.work_mode}"


@dataclass(slots=True)
class DiscoveryConfig:
    """Fully-resolved worker config. The two `*_selectors` / `matrix` members
    are swapped atomically on SIGHUP (see worker entrypoint)."""

    idle_sec: int
    backoff_sec: int
    comp_floor_inr: float
    max_cycles_per_engine: int
    identity_label: str
    pages_per_url: int
    selectors_path: Path
    matrix_path: Path
    selectors: dict[str, Any] = field(default_factory=dict)
    selectors_version: str = ""
    matrix: list[Combo] = field(default_factory=list)
    matrix_version: str = ""
    # CLI-driven flags (set by `src/cli/internshala_discover.py`); worker defaults.
    once: bool = False
    dry_run: bool = False
    combo_filter: str | None = None

    @property
    def listing_selectors(self) -> dict[str, str]:
        """The `selectors.listing` subtree — passed to `parse_card`."""
        return dict(self.selectors.get("listing", {}))

    def active_combos(self) -> list[Combo]:
        """Matrix filtered by `--combo` when set, else the whole matrix."""
        if self.combo_filter is None:
            return list(self.matrix)
        return [c for c in self.matrix if c.name == self.combo_filter]


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        _log.warning("discovery_env_int_invalid", var=name, value=raw, fallback=default)
        return default


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data or {}


def _prefs_overrides() -> dict[str, Any]:
    """`prefs.yaml -> discovery.internshala` block, or {} when absent/unreadable.

    Missing file / malformed YAML is non-fatal — the worker falls back to env.
    """
    if not _PREFS_PATH.exists():
        return {}
    try:
        prefs = _read_yaml(_PREFS_PATH)
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning("discovery_prefs_read_failed", path=str(_PREFS_PATH), err=str(exc))
        return {}
    discovery = prefs.get("discovery") or {}
    block = discovery.get("internshala") or {}
    return dict(block) if isinstance(block, dict) else {}


def expand_matrix(matrix_doc: dict[str, Any]) -> tuple[list[Combo], str]:
    """Expand the dropdown-matrix YAML doc into `(combos, version)`.

    Rejects rows missing `category` / `work_mode` or carrying unknown axes, so a
    matrix typo fails loudly rather than silently skipping a combo.
    """
    version = str(matrix_doc.get("version", ""))
    rows = matrix_doc.get("matrix") or []
    combos: list[Combo] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"matrix[{i}] is not a mapping: {row!r}")
        extra = set(row) - {"category", "work_mode"}
        if extra:
            raise ValueError(f"matrix[{i}] has unknown axes {sorted(extra)}; allowed: category, work_mode")
        if "category" not in row or "work_mode" not in row:
            raise ValueError(f"matrix[{i}] missing required axis (category, work_mode): {row!r}")
        combos.append(Combo(category=str(row["category"]), work_mode=str(row["work_mode"])))
    return combos, version


def load_selectors(path: Path) -> tuple[dict[str, Any], str]:
    """Load `internshala_selectors.yaml` -> `(selectors_subtree, version)`."""
    doc = _read_yaml(path)
    version = str(doc.get("version", ""))
    selectors = doc.get("selectors") or {}
    if not isinstance(selectors, dict):
        raise ValueError(f"{path}: `selectors` must be a mapping")
    return selectors, version


def _guard_recon_pending(version: str) -> None:
    """Refuse to boot on the RECON_PENDING placeholder unless explicitly allowed.

    `INTERNSHALA_ALLOW_RECON_PENDING=1` is the escape hatch for `--dry-run` smoke
    against placeholders; production deploys MUST bump `selectors.version`.
    """
    if version != RECON_PENDING_SENTINEL:
        return
    if os.environ.get("INTERNSHALA_ALLOW_RECON_PENDING") == "1":
        _log.warning(
            "discovery_selectors_recon_pending_allowed",
            detail="RECON_PENDING allowed via INTERNSHALA_ALLOW_RECON_PENDING=1 (dry-run/smoke only)",
        )
        return
    raise ReconPendingError(
        "internshala_selectors.yaml still has version=RECON_PENDING. "
        "Capture live selectors on the ThinkPad and bump the version before deploy, "
        "or set INTERNSHALA_ALLOW_RECON_PENDING=1 for a --dry-run smoke."
    )


def load_config(
    *,
    once: bool = False,
    dry_run: bool = False,
    combo_filter: str | None = None,
) -> DiscoveryConfig:
    """Build the resolved `DiscoveryConfig` from env + prefs + the two YAMLs.

    Raises `ReconPendingError` (a SystemExit subclass) when the selector YAML is
    still RECON_PENDING and the allow-env is unset. CLI flags (`once`,
    `dry_run`, `combo_filter`) are threaded through verbatim.
    """
    prefs = _prefs_overrides()

    def _resolved_int(prefs_key: str, env_name: str, default: int) -> int:
        env_val = _env_int(env_name, default)
        # prefs.yaml wins over env when present (operator-facing override).
        if prefs_key in prefs and prefs[prefs_key] is not None:
            try:
                return int(prefs[prefs_key])
            except (TypeError, ValueError):
                _log.warning("discovery_prefs_int_invalid", key=prefs_key, value=prefs[prefs_key], fallback=env_val)
        return env_val

    selectors_path = Path(os.environ.get("INTERNSHALA_SELECTORS_PATH") or _DEFAULT_SELECTORS_PATH)
    matrix_path = Path(os.environ.get("INTERNSHALA_MATRIX_PATH") or _DEFAULT_MATRIX_PATH)

    selectors, selectors_version = load_selectors(selectors_path)
    _guard_recon_pending(selectors_version)

    combos, matrix_version = expand_matrix(_read_yaml(matrix_path))

    cfg = DiscoveryConfig(
        idle_sec=_resolved_int("idle_sec", "INTERNSHALA_IDLE_SEC", _DEFAULT_IDLE_SEC),
        backoff_sec=_env_int("INTERNSHALA_BACKOFF_SEC", _DEFAULT_BACKOFF_SEC),
        comp_floor_inr=float(_resolved_int("comp_floor_inr", "INTERNSHALA_COMP_FLOOR_INR", _DEFAULT_COMP_FLOOR_INR)),
        max_cycles_per_engine=_resolved_int("max_cycles_per_engine", "INTERNSHALA_MAX_CYCLES_PER_ENGINE", _DEFAULT_MAX_CYCLES_PER_ENGINE),
        identity_label=os.environ.get("INTERNSHALA_IDENTITY_LABEL", _DEFAULT_IDENTITY_LABEL),
        pages_per_url=_env_int("INTERNSHALA_PAGES_PER_URL", _DEFAULT_PAGES_PER_URL),
        selectors_path=selectors_path,
        matrix_path=matrix_path,
        selectors=selectors,
        selectors_version=selectors_version,
        matrix=combos,
        matrix_version=matrix_version,
        once=once,
        dry_run=dry_run,
        combo_filter=combo_filter,
    )
    _log.info(
        "discovery_config_loaded",
        idle_sec=cfg.idle_sec,
        comp_floor_inr=cfg.comp_floor_inr,
        combos=len(cfg.matrix),
        selectors_version=cfg.selectors_version,
        matrix_version=cfg.matrix_version,
        dry_run=cfg.dry_run,
        once=cfg.once,
    )
    return cfg


def reload_into(cfg: DiscoveryConfig) -> None:
    """Re-read both YAMLs and swap the selector / matrix members in place (SIGHUP).

    A bad YAML keeps the old values + logs `selectors_reload_failed` — a
    fat-fingered selector edit must never take discovery offline. The
    RECON_PENDING guard is intentionally NOT re-applied: a running worker already
    cleared it at boot, so a mid-flight flip back to the placeholder degrades to
    "keep old selectors", it does not crash the loop.
    """
    try:
        selectors, selectors_version = load_selectors(cfg.selectors_path)
        combos, matrix_version = expand_matrix(_read_yaml(cfg.matrix_path))
    except Exception as exc:
        _log.warning("selectors_reload_failed", err=str(exc))
        return
    old_sel, old_matrix = cfg.selectors_version, cfg.matrix_version
    cfg.selectors = selectors
    cfg.selectors_version = selectors_version
    cfg.matrix = combos
    cfg.matrix_version = matrix_version
    _log.info(
        "selectors_reloaded",
        old_selectors=old_sel,
        new_selectors=selectors_version,
        old_matrix=old_matrix,
        new_matrix=matrix_version,
    )


__all__ = [
    "IDENTITY_PLATFORM",
    "INTERNSHALA_LISTING_URL",
    "RECON_PENDING_SENTINEL",
    "SOURCE_SLUG",
    "Combo",
    "DiscoveryConfig",
    "ReconPendingError",
    "expand_matrix",
    "load_config",
    "load_selectors",
    "reload_into",
]
