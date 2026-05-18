"""Tests for the keyword-vote block selector."""

from __future__ import annotations

import pytest

from src.application.resume_latex.parser.blocks import Block
from src.application.resume_latex.selector import rank


def _block(title: str, bullets: list[str], kind: str = "event") -> Block:
    return Block(
        id=title.lower().replace(" ", "_"),
        kind=kind,
        title=title,
        bullets=bullets,
        file="mmayer.tex",
        char_range=(0, 100),
    )


@pytest.mark.smoke
def test_keyword_match_ranks_relevant_block_first():
    blocks = [
        _block("Frontend Designer", ["Built design system", "Tailwind CSS work"]),
        _block("Backend SDE Intern", ["Built Python APIs", "PostgreSQL schema"]),
    ]
    opp = {
        "title": "Backend Python Engineer",
        "description": "PostgreSQL, async, distributed systems",
    }
    ranked = rank(blocks, opp)
    assert ranked[0].title == "Backend SDE Intern"


def test_handles_dict_opp_and_model_opp():
    """Accepts both an Opportunity model and a dict — duck-typed."""
    blocks = [_block("X", ["python"]), _block("Y", ["java"])]

    class FakeOpp:
        title = "python role"
        description = ""

    by_dict = rank(blocks, {"title": "python role", "description": ""})
    by_model = rank(blocks, FakeOpp())
    assert by_dict[0].title == by_model[0].title == "X"


def test_variant_keywords_augment_signal():
    blocks = [
        _block("A", ["wrote rust services"]),
        _block("B", ["wrote python services"]),
    ]
    opp = {"title": "Engineer", "description": ""}
    # No signal in either => stable order, A wins.
    base = rank(blocks, opp)
    assert base[0].title == "A"
    # Variant boost flips it.
    boosted = rank(blocks, opp, variant_keywords=["python"])
    assert boosted[0].title == "B"


def test_no_keywords_keeps_parse_order():
    blocks = [_block("X", []), _block("Y", []), _block("Z", [])]
    out = rank(blocks, {"title": "", "description": ""})
    assert [b.title for b in out] == ["X", "Y", "Z"]


def test_stable_sort_on_tie():
    blocks = [_block("First", ["python"]), _block("Second", ["python"])]
    out = rank(blocks, {"title": "python", "description": ""})
    # Both score 1 — stable sort preserves input order.
    assert out[0].title == "First"
    assert out[1].title == "Second"


def test_case_insensitive_matching():
    blocks = [_block("Engineering Role", ["DELIVERED features"])]
    out = rank(blocks, {"title": "delivered", "description": ""})
    # 'delivered' matches DELIVERED — lower-case fold both sides.
    assert out[0].title == "Engineering Role"
