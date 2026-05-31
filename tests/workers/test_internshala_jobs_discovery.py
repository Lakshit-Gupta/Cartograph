"""Hermetic unit tests for the Internshala JOBS discovery worker's pure logic.

No browser / Redis / Postgres. Covers:
  - `build_variants`: URL construction for both /jobs/ and /fresher-jobs/ paths,
    city encoding, work-from-home + salary path segments, single-variant cases.
  - `passes_salary_floor`: STRICT comp_min ≥ floor (differs from internships),
    currency conversion, range-min-below-floor drop.
  - `passes_experience`: min-years ≤ cap, fail-open on None.
  - `load_jobs_config`: prefs > env > default; derived salary_floor_inr.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.common.types import OppCategory, Opportunity, RemoteType
from src.workers.internshala_discovery.browser_ops import page_url
from src.workers.internshala_jobs_discovery import config as cfg_mod
from src.workers.internshala_jobs_discovery.config import (
    JobVariant,
    build_variants,
    load_jobs_config,
)
from src.workers.internshala_jobs_discovery.filters import passes_experience, passes_keywords, passes_salary_floor

_CITIES = ["bangalore", "gurgaon", "pune", "uttar-pradesh", "ghaziabad"]


def _opp(*, comp_min=None, comp_max=None, currency="INR", period="year", years=None, title="Backend Engineer") -> Opportunity:
    return Opportunity(
        source_id=1,
        canonical_url="https://internshala.com/job/x",
        title=title,
        comp_min=comp_min,
        comp_max=comp_max,
        comp_currency=currency,
        comp_period=period,
        category=OppCategory.FULLTIME,
        remote_type=RemoteType.REMOTE,
        years_experience_min=years,
        fingerprint_hash="fp",
    )


# --------------------------------------------------------------------------- #
# passes_keywords — title must match an include term (if any) AND no exclude.
# --------------------------------------------------------------------------- #
_INCLUDE = ["backend", "python", "full stack", "machine learning", "ml", "software", "engineer", "data scientist"]
_EXCLUDE = ["sales", "marketing", "mechanical", "civil", "gynecologist"]


@pytest.mark.smoke
@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Backend Developer", True),  # include 'backend'
        ("Full Stack Developer", True),  # include phrase 'full stack'
        ("ML Engineer", True),  # include 'ml' (word-boundary on 'ML')
        ("Data Scientist", True),  # include 'data scientist'
        ("Sales Executive", False),  # no include term -> drop
        ("Sales Engineer", False),  # include 'engineer' BUT exclude 'sales' wins
        ("Mechanical Engineer", False),  # include 'engineer' BUT exclude 'mechanical' wins
        ("Gynecologist", False),  # exclude + no include
        ("Digital Marketing Manager", False),  # exclude 'marketing'
        ("Software Engineer", True),  # include 'software'
    ],
)
def test_passes_keywords(title, expected) -> None:
    assert passes_keywords(_opp(title=title), _INCLUDE, _EXCLUDE) is expected


@pytest.mark.smoke
def test_passes_keywords_empty_lists_keep_all() -> None:
    # No include list + no exclude list -> no filtering (keep everything).
    assert passes_keywords(_opp(title="Sales Executive"), [], []) is True


@pytest.mark.smoke
def test_passes_keywords_exclude_only() -> None:
    # No include list, exclude only -> keep unless excluded.
    assert passes_keywords(_opp(title="Sales Executive"), [], ["sales"]) is False
    assert passes_keywords(_opp(title="Backend Developer"), [], ["sales"]) is True


# --------------------------------------------------------------------------- #
# build_variants — URL construction.
# --------------------------------------------------------------------------- #
def _urls(**kw) -> dict[str, str]:
    defaults = dict(work_from_home=True, min_salary_lpa_url=10, crawl_fresher=True, crawl_general=True)
    defaults.update(kw)
    return {v.name: v.url for v in build_variants(_CITIES, **defaults)}


@pytest.mark.smoke
def test_both_variants_emitted() -> None:
    urls = _urls()
    assert set(urls) == {"general", "fresher"}
    assert urls["general"] == (
        "https://internshala.com/jobs/jobs-in-bangalore,gurgaon,pune,uttar-pradesh,ghaziabad/work-from-home/salary-10"
    )
    assert urls["fresher"] == (
        "https://internshala.com/fresher-jobs/jobs-in-bangalore,gurgaon,pune,uttar-pradesh,ghaziabad/work-from-home/salary-10"
    )


@pytest.mark.smoke
def test_city_encoding_lowercases_and_hyphenates() -> None:
    urls = _urls()
    assert "jobs-in-bangalore,gurgaon,pune,uttar-pradesh,ghaziabad" in urls["general"]
    # A space-separated city name is slugified.
    spaced = {
        v.name: v.url
        for v in build_variants(
            ["Uttar Pradesh", "New Delhi"], work_from_home=True, min_salary_lpa_url=10, crawl_fresher=False, crawl_general=True
        )
    }
    assert "jobs-in-uttar-pradesh,new-delhi" in spaced["general"]


@pytest.mark.smoke
def test_work_from_home_segment_toggles() -> None:
    assert "/work-from-home/" in _urls(work_from_home=True)["general"]
    assert "work-from-home" not in _urls(work_from_home=False)["general"]


@pytest.mark.smoke
def test_salary_segment_uses_url_param() -> None:
    assert _urls(min_salary_lpa_url=10)["general"].endswith("/salary-10")
    assert _urls(min_salary_lpa_url=8)["general"].endswith("/salary-8")


@pytest.mark.smoke
def test_only_fresher_variant() -> None:
    urls = _urls(crawl_fresher=True, crawl_general=False)
    assert set(urls) == {"fresher"}


@pytest.mark.smoke
def test_only_general_variant() -> None:
    urls = _urls(crawl_fresher=False, crawl_general=True)
    assert set(urls) == {"general"}


@pytest.mark.smoke
def test_variant_is_named_tuple_like() -> None:
    v = build_variants(_CITIES, work_from_home=True, min_salary_lpa_url=10, crawl_fresher=True, crawl_general=False)[0]
    assert isinstance(v, JobVariant)
    assert v.name == "fresher"
    assert v.url.startswith("https://internshala.com/fresher-jobs/")


@pytest.mark.smoke
def test_variant_url_paginates_with_page_n() -> None:
    # Jobs paginate by appending /page-N/ to the variant URL (same primitive as
    # internships) — the "Load more" button was retired.
    v = build_variants(_CITIES, work_from_home=True, min_salary_lpa_url=10, crawl_fresher=False, crawl_general=True)[0]
    assert page_url(v.url, 1) == v.url + "/"
    assert page_url(v.url, 3) == v.url + "/page-3/"


# --------------------------------------------------------------------------- #
# passes_salary_floor — STRICT comp_min ≥ floor (12 LPA = 100000 INR/mo).
# --------------------------------------------------------------------------- #
@pytest.mark.smoke
@pytest.mark.parametrize(
    ("comp_min", "comp_max", "currency", "period", "expected"),
    [
        (1_200_000, None, "INR", "year", True),  # 12 LPA == floor (inclusive)
        (1_500_000, None, "INR", "year", True),  # 15 LPA above floor
        (600_000, None, "INR", "year", False),  # 6 LPA below floor
        (600_000, 1_800_000, "INR", "year", False),  # range min 6 LPA -> STRICT drop (max ignored)
        (1_200_000, 2_400_000, "INR", "year", True),  # range min 12 LPA clears
        (None, 1_800_000, "INR", "year", False),  # no comp_min -> drop
        (70_000, None, "USD", "year", True),  # 70k USD/yr -> ~4.8L/mo INR
        (10_000, None, "USD", "year", False),  # 10k USD/yr -> ~69k/mo < 100k
        (1_500_000, None, "XYZ", "year", False),  # unknown currency -> drop
    ],
)
def test_passes_salary_floor_strict_min(comp_min, comp_max, currency, period, expected) -> None:
    opp = _opp(comp_min=comp_min, comp_max=comp_max, currency=currency, period=period)
    assert passes_salary_floor(opp, 100_000) is expected


# --------------------------------------------------------------------------- #
# passes_experience — keep iff min-years ≤ cap; fail-open on None.
# --------------------------------------------------------------------------- #
@pytest.mark.smoke
@pytest.mark.parametrize(
    ("years", "cap", "expected"),
    [
        (0, 1, True),
        (1, 1, True),
        (2, 1, False),
        (3, 2, False),
        (2, 2, True),
        (None, 1, True),  # fail-open
    ],
)
def test_passes_experience(years, cap, expected) -> None:
    assert passes_experience(_opp(years=years), cap) is expected


# --------------------------------------------------------------------------- #
# load_jobs_config — prefs > env > default + derived salary_floor_inr.
# --------------------------------------------------------------------------- #
def _write_selectors(path: Path, version: str) -> None:
    doc = {
        "version": version,
        "selectors": {
            "page_root": "body",
            "listing": {"card_root": "div.individual_internship", "card_title": ".t"},
        },
    }
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")


@pytest.mark.smoke
def test_config_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sel = tmp_path / "jobs_sel.yaml"
    _write_selectors(sel, "v1")
    monkeypatch.setenv("INTERNSHALA_JOBS_SELECTORS_PATH", str(sel))
    monkeypatch.setattr(cfg_mod, "_prefs_overrides", dict)
    monkeypatch.delenv("INTERNSHALA_JOBS_SALARY_FLOOR_LPA", raising=False)
    monkeypatch.delenv("INTERNSHALA_JOBS_MAX_EXPERIENCE_YEARS", raising=False)
    cfg = load_jobs_config()
    assert cfg.salary_floor_lpa == 12
    assert cfg.max_experience_years == 1
    assert cfg.salary_floor_inr == pytest.approx(100_000.0)  # 12 LPA / 12


@pytest.mark.smoke
def test_config_prefs_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sel = tmp_path / "jobs_sel.yaml"
    _write_selectors(sel, "v1")
    monkeypatch.setenv("INTERNSHALA_JOBS_SELECTORS_PATH", str(sel))
    monkeypatch.setattr(cfg_mod, "_prefs_overrides", lambda: {"salary_floor_lpa": 15, "max_experience_years": 0})
    cfg = load_jobs_config()
    assert cfg.salary_floor_lpa == 15
    assert cfg.max_experience_years == 0
    assert cfg.salary_floor_inr == pytest.approx(125_000.0)  # 15 LPA / 12


@pytest.mark.smoke
def test_config_recon_pending_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sel = tmp_path / "jobs_sel.yaml"
    _write_selectors(sel, "RECON_PENDING")
    monkeypatch.setenv("INTERNSHALA_JOBS_SELECTORS_PATH", str(sel))
    monkeypatch.delenv("INTERNSHALA_JOBS_ALLOW_RECON_PENDING", raising=False)
    monkeypatch.setattr(cfg_mod, "_prefs_overrides", dict)
    with pytest.raises(cfg_mod.ReconPendingError):
        load_jobs_config()
