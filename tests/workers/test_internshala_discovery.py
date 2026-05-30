"""Hermetic unit tests for the Internshala discovery worker's pure logic.

No browser / Redis / Postgres. Covers the side-effect-free pieces:
  - config loader: both YAMLs parse; RECON_PENDING guard refuses without the
    allow-env and passes with it; env + prefs precedence.
  - combo expansion: the shipped matrix YAML expands to exactly 12 combos;
    unknown axes + missing axes are rejected.
  - floor filter (`passes_floor`): comp value x currency/period table.
  - dedup key purity + determinism.
  - `DiscoveryCycleReport` -> row / details round-trip + SQL column mapping.
  - cycle-report notify payload shape (FROZEN keys the Discord handler reads).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.common.types import OppCategory, Opportunity, RemoteType
from src.workers.internshala_discovery import config as cfg_mod
from src.workers.internshala_discovery.config import (
    RECON_PENDING_SENTINEL,
    Combo,
    ReconPendingError,
    expand_matrix,
    load_config,
)
from src.workers.internshala_discovery.report import (
    CYCLE_REPORT_KIND,
    DiscoveryCycleReport,
    build_cycle_report_payload,
    build_summary,
    dedup_key,
    passes_floor,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SHIPPED_SELECTORS = _REPO_ROOT / "config" / "sources" / "internshala_selectors.yaml"
_SHIPPED_MATRIX = _REPO_ROOT / "config" / "sources" / "internshala_dropdown_matrix.yaml"

_EXPECTED_COMBO_COUNT = 12


def _opp(*, comp_min=None, comp_max=None, currency="INR", period="month", url="https://internshala.com/x") -> Opportunity:
    return Opportunity(
        source_id=1,
        canonical_url=url,
        title="Backend Developer",
        comp_min=comp_min,
        comp_max=comp_max,
        comp_currency=currency,
        comp_period=period,
        category=OppCategory.INTERNSHIP,
        remote_type=RemoteType.REMOTE,
        fingerprint_hash="fp",
    )


def _write_selectors(path: Path, version: str) -> None:
    doc = {
        "version": version,
        "selectors": {
            "page_root": "body",
            "listing": {"card_root": "div.individual_internship", "card_title": ".t"},
            "dropdown": {"stipend_button": "#s"},
        },
    }
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")


def _write_matrix(path: Path) -> None:
    doc = {
        "version": "test.v1",
        "matrix": [
            {"category": "Backend Development", "work_mode": "wfh"},
            {"category": "Machine Learning", "work_mode": "wfh"},
        ],
    }
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Shipped YAMLs parse + expand to the documented combo set.
# --------------------------------------------------------------------------- #
@pytest.mark.smoke
def test_shipped_yamls_parse() -> None:
    selectors = yaml.safe_load(_SHIPPED_SELECTORS.read_text(encoding="utf-8"))
    matrix = yaml.safe_load(_SHIPPED_MATRIX.read_text(encoding="utf-8"))
    assert selectors["version"] == RECON_PENDING_SENTINEL  # ships pending recon
    assert "listing" in selectors["selectors"]
    assert matrix["version"]
    assert len(matrix["matrix"]) == _EXPECTED_COMBO_COUNT


@pytest.mark.smoke
def test_shipped_matrix_expands_to_twelve_combos() -> None:
    doc = yaml.safe_load(_SHIPPED_MATRIX.read_text(encoding="utf-8"))
    combos, version = expand_matrix(doc)
    assert version == "2026.05.29.v1"
    assert len(combos) == _EXPECTED_COMBO_COUNT
    assert all(isinstance(c, Combo) for c in combos)
    assert all(c.work_mode == "wfh" for c in combos)
    # Names are unique + filesystem-safe (no spaces / slashes / parens).
    names = [c.name for c in combos]
    assert len(set(names)) == _EXPECTED_COMBO_COUNT
    for n in names:
        assert not (set(n) & set(" /()."))
    assert "backend-development-wfh" in names
    assert "artificial-intelligence-ai-wfh" in names


@pytest.mark.smoke
def test_expand_matrix_rejects_unknown_and_missing_axes() -> None:
    with pytest.raises(ValueError, match="unknown axes"):
        expand_matrix({"version": "v", "matrix": [{"category": "X", "work_mode": "wfh", "city": "BLR"}]})
    with pytest.raises(ValueError, match="missing required axis"):
        expand_matrix({"version": "v", "matrix": [{"category": "X"}]})


# --------------------------------------------------------------------------- #
# RECON_PENDING guard + env/prefs precedence in load_config.
# --------------------------------------------------------------------------- #
@pytest.mark.smoke
def test_recon_pending_guard_refuses_without_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sel = tmp_path / "sel.yaml"
    mat = tmp_path / "mat.yaml"
    _write_selectors(sel, RECON_PENDING_SENTINEL)
    _write_matrix(mat)
    monkeypatch.setenv("INTERNSHALA_SELECTORS_PATH", str(sel))
    monkeypatch.setenv("INTERNSHALA_MATRIX_PATH", str(mat))
    monkeypatch.delenv("INTERNSHALA_ALLOW_RECON_PENDING", raising=False)
    # prefs override must not be consulted (would touch the real file) — neuter it.
    monkeypatch.setattr(cfg_mod, "_prefs_overrides", dict)
    with pytest.raises(ReconPendingError):
        load_config()


@pytest.mark.smoke
def test_recon_pending_guard_passes_with_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sel = tmp_path / "sel.yaml"
    mat = tmp_path / "mat.yaml"
    _write_selectors(sel, RECON_PENDING_SENTINEL)
    _write_matrix(mat)
    monkeypatch.setenv("INTERNSHALA_SELECTORS_PATH", str(sel))
    monkeypatch.setenv("INTERNSHALA_MATRIX_PATH", str(mat))
    monkeypatch.setenv("INTERNSHALA_ALLOW_RECON_PENDING", "1")
    monkeypatch.setattr(cfg_mod, "_prefs_overrides", dict)
    cfg = load_config(once=True, dry_run=True)
    assert cfg.selectors_version == RECON_PENDING_SENTINEL
    assert cfg.once is True
    assert cfg.dry_run is True
    assert len(cfg.matrix) == 2


@pytest.mark.smoke
def test_load_config_bumped_version_does_not_need_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sel = tmp_path / "sel.yaml"
    mat = tmp_path / "mat.yaml"
    _write_selectors(sel, "2026.05.29.v1")
    _write_matrix(mat)
    monkeypatch.setenv("INTERNSHALA_SELECTORS_PATH", str(sel))
    monkeypatch.setenv("INTERNSHALA_MATRIX_PATH", str(mat))
    monkeypatch.delenv("INTERNSHALA_ALLOW_RECON_PENDING", raising=False)
    monkeypatch.setattr(cfg_mod, "_prefs_overrides", dict)
    cfg = load_config()
    assert cfg.selectors_version == "2026.05.29.v1"


@pytest.mark.smoke
def test_prefs_override_wins_over_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sel = tmp_path / "sel.yaml"
    mat = tmp_path / "mat.yaml"
    _write_selectors(sel, "v1")
    _write_matrix(mat)
    monkeypatch.setenv("INTERNSHALA_SELECTORS_PATH", str(sel))
    monkeypatch.setenv("INTERNSHALA_MATRIX_PATH", str(mat))
    monkeypatch.setenv("INTERNSHALA_IDLE_SEC", "180")
    monkeypatch.setenv("INTERNSHALA_COMP_FLOOR_INR", "30000")
    # prefs supplies a different idle_sec + floor — prefs must win.
    monkeypatch.setattr(cfg_mod, "_prefs_overrides", lambda: {"idle_sec": 1800, "comp_floor_inr": 45000})
    cfg = load_config()
    assert cfg.idle_sec == 1800
    assert cfg.comp_floor_inr == 45000.0


@pytest.mark.smoke
def test_env_used_when_prefs_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sel = tmp_path / "sel.yaml"
    mat = tmp_path / "mat.yaml"
    _write_selectors(sel, "v1")
    _write_matrix(mat)
    monkeypatch.setenv("INTERNSHALA_SELECTORS_PATH", str(sel))
    monkeypatch.setenv("INTERNSHALA_MATRIX_PATH", str(mat))
    monkeypatch.setenv("INTERNSHALA_IDLE_SEC", "999")
    monkeypatch.setattr(cfg_mod, "_prefs_overrides", dict)
    cfg = load_config()
    assert cfg.idle_sec == 999


@pytest.mark.smoke
def test_combo_filter_selects_single_combo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sel = tmp_path / "sel.yaml"
    mat = tmp_path / "mat.yaml"
    _write_selectors(sel, "v1")
    _write_matrix(mat)
    monkeypatch.setenv("INTERNSHALA_SELECTORS_PATH", str(sel))
    monkeypatch.setenv("INTERNSHALA_MATRIX_PATH", str(mat))
    monkeypatch.setattr(cfg_mod, "_prefs_overrides", dict)
    cfg = load_config(combo_filter="machine-learning-wfh")
    active = cfg.active_combos()
    assert len(active) == 1
    assert active[0].category == "Machine Learning"


# --------------------------------------------------------------------------- #
# Floor filter table.
# --------------------------------------------------------------------------- #
@pytest.mark.smoke
@pytest.mark.parametrize(
    ("comp_min", "comp_max", "currency", "period", "expected"),
    [
        (None, None, "INR", "month", False),  # no stipend -> drop
        (5_000, None, "INR", "month", False),  # below floor
        (29_999, None, "INR", "month", False),  # just below floor
        (30_000, None, "INR", "month", True),  # exactly at floor
        (80_000, None, "INR", "month", True),  # above floor
        (15_000, 35_000, "INR", "month", True),  # range upper clears floor
        (10_000, 20_000, "INR", "month", False),  # range upper below floor
        (500, None, "INR", "hour", True),  # 500/hr -> 80k/mo
        (50_000, None, "USD", "year", True),  # 50k USD/yr -> ~3.4L/mo INR
        (1_000, None, "XYZ", "month", False),  # unknown currency -> drop
    ],
)
def test_passes_floor_table(comp_min, comp_max, currency, period, expected) -> None:
    opp = _opp(comp_min=comp_min, comp_max=comp_max, currency=currency, period=period)
    assert passes_floor(opp, 30_000) is expected


# --------------------------------------------------------------------------- #
# Dedup key purity.
# --------------------------------------------------------------------------- #
@pytest.mark.smoke
def test_dedup_key_is_pure_and_prefixed() -> None:
    url = "https://internshala.com/internship/detail/abc-123"
    k1 = dedup_key(url)
    k2 = dedup_key(url)
    assert k1 == k2  # deterministic
    assert k1.startswith("internshala:seen:")
    assert dedup_key(url) != dedup_key(url + "x")  # collision-free for distinct URLs
    # The suffix is a hex sha256 (64 chars).
    assert len(k1.split(":")[-1]) == 64


# --------------------------------------------------------------------------- #
# Report round-trip + SQL column mapping.
# --------------------------------------------------------------------------- #
def _report() -> DiscoveryCycleReport:
    return DiscoveryCycleReport(
        cycle_id="11111111-1111-1111-1111-111111111111",
        worker_id="discovery-test-1",
        source_slug="in_internshala",
        started_at="2026-05-29T10:00:00+00:00",
        duration_sec=192.4,
        combos_attempted=12,
        combos_succeeded=11,
        combo_timeouts=["devops-wfh"],
        selector_misses=["data-science-wfh:dropdown.category_options"],
        cards_scraped=120,
        cards_published=47,
        cards_rejected_subfloor=50,
        cards_rejected_dedup=20,
        cards_rejected_parse=3,
        healthy=True,
        selectors_version="2026.05.29.v1",
        matrix_version="2026.05.29.v1",
    )


# The exact column list of discovery_cycle_log (V026) — the row dict MUST carry
# every column the worker's INSERT names (minus DB-managed id / created_at).
_EXPECTED_ROW_KEYS = {
    "cycle_id",
    "worker_id",
    "source_slug",
    "started_at",
    "duration_sec",
    "combos_attempted",
    "combos_succeeded",
    "combo_timeouts",
    "selector_misses",
    "cards_scraped",
    "cards_published",
    "cards_rejected_subfloor",
    "cards_rejected_dedup",
    "cards_rejected_parse",
    "healthy",
    "selectors_version",
    "matrix_version",
}


@pytest.mark.smoke
def test_report_to_row_covers_sql_columns() -> None:
    row = _report().to_row()
    assert set(row) == _EXPECTED_ROW_KEYS
    assert row["combo_timeouts"] == ["devops-wfh"]
    assert row["cards_published"] == 47
    assert row["healthy"] is True


@pytest.mark.smoke
def test_report_details_round_trip() -> None:
    rep = _report()
    details = rep.to_details()
    # details is a JSON-safe projection identical to the row mapping today.
    assert details == rep.to_row()
    assert details["selector_misses"] == ["data-science-wfh:dropdown.category_options"]


# --------------------------------------------------------------------------- #
# Cycle-report notify payload shape (FROZEN contract).
# --------------------------------------------------------------------------- #
@pytest.mark.smoke
def test_cycle_report_payload_shape() -> None:
    rep = _report()
    payload = build_cycle_report_payload(rep, screenshot_b64=None)
    assert payload["kind"] == CYCLE_REPORT_KIND == "discovery_cycle_report"
    assert payload["cycle_id"] == rep.cycle_id
    assert payload["source_slug"] == "in_internshala"
    assert payload["started_at"] == rep.started_at
    assert payload["duration_sec"] == rep.duration_sec
    assert payload["healthy"] is True
    assert payload["screenshot_b64"] is None
    assert isinstance(payload["summary"], str) and payload["summary"]
    assert payload["details"] == rep.to_details()
    # Every FROZEN top-level key present.
    assert set(payload) == {
        "kind",
        "cycle_id",
        "source_slug",
        "started_at",
        "duration_sec",
        "summary",
        "healthy",
        "screenshot_b64",
        "details",
    }


@pytest.mark.smoke
def test_summary_healthy_vs_degraded() -> None:
    healthy = build_summary(_report())
    assert healthy.startswith("✓")
    assert "47 cards" in healthy
    assert "11/12 combos" in healthy
    assert "3m12s" in healthy

    rep = _report()
    rep.healthy = False
    rep.cards_published = 0
    degraded = build_summary(rep)
    assert degraded.startswith("✗")
    # timeouts + selector-miss counts surface on a degraded line.
    assert "timeouts" in degraded
