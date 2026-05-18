"""Tests for the LaTeX bullet sanitizer.

The sanitizer is the safety boundary between the LLM and tectonic. It must:
1. Reject any forbidden macro outright (denylist).
2. Strip any non-allowlisted macro silently (still escape the bullet).
3. Escape every LaTeX special so the renderer can splice safely.
"""
from __future__ import annotations

import pytest

from src.application.resume_latex.sanitizer import SanitizerReject, escape_and_check

# --- denylist rejection ------------------------------------------------------

@pytest.mark.smoke
def test_rejects_write18():
    with pytest.raises(SanitizerReject, match=r"\\write18"):
        escape_and_check([r"Did things \write18{rm -rf /}"])


@pytest.mark.smoke
def test_rejects_input():
    with pytest.raises(SanitizerReject, match=r"\\input"):
        escape_and_check([r"Loaded \input{/etc/passwd}"])


def test_rejects_catcode():
    with pytest.raises(SanitizerReject, match=r"\\catcode"):
        escape_and_check([r"Tweaked \catcode`@=11"])


def test_rejects_csname_def_let_expandafter():
    for forbidden in (r"\csname foo \endcsname", r"\def\foo{bar}", r"\let\x=\y",
                      r"\expandafter\foo"):
        with pytest.raises(SanitizerReject):
            escape_and_check([forbidden])


def test_rejects_directlua():
    with pytest.raises(SanitizerReject, match=r"\\directlua"):
        escape_and_check([r"Ran \directlua{os.execute('ls')}"])


def test_rejects_loop():
    with pytest.raises(SanitizerReject):
        escape_and_check([r"\loop forever"])


def test_rejects_immediate_openin_openout_read():
    for forbidden in (r"\immediate\write", r"\openin0=file", r"\openout1=file", r"\read1 to\foo"):
        with pytest.raises(SanitizerReject):
            escape_and_check([forbidden])


# --- escape behavior ---------------------------------------------------------

def test_escapes_special_chars():
    out = escape_and_check(["50% improvement & saved $1M"])
    assert r"\%" in out[0]
    assert r"\&" in out[0]
    assert r"\$" in out[0]


def test_escapes_hash_underscore_braces():
    out = escape_and_check(["Used # for headings and _ for spaces in {dict} keys"])
    assert r"\#" in out[0]
    assert r"\_" in out[0]
    assert r"\{" in out[0]
    assert r"\}" in out[0]


def test_escapes_tilde_caret():
    out = escape_and_check(["a~b^c"])
    assert r"\textasciitilde{}" in out[0]
    assert r"\textasciicircum{}" in out[0]


# --- allowlist preservation --------------------------------------------------

def test_allows_textbf_textit_emph_unchanged():
    out = escape_and_check([r"Used \textbf{tools} and \textit{methods} with \emph{care}"])
    bullet = out[0]
    # The three allowlisted macros must still be present (sentinel round-trip).
    assert r"\textbf" in bullet
    assert r"\textit" in bullet
    assert r"\emph" in bullet
    # Braces inside the bullet were escaped, so the allowlisted command's
    # argument braces are visible — they survived the sentinel swap.


def test_strips_unallowed_commands():
    out = escape_and_check([r"Used \customMacro{foo} in production"])
    # \customMacro is removed; its argument text remains (inside escaped braces).
    assert r"\customMacro" not in out[0]
    assert "foo" in out[0]


def test_strips_section_href_etc():
    out = escape_and_check([r"Saw \section{intro} and \href{u}{v}"])
    assert r"\section" not in out[0]
    assert r"\href" not in out[0]


# --- edge cases --------------------------------------------------------------

def test_empty_input_returns_empty_output():
    assert escape_and_check([]) == []


def test_plain_text_passes_through():
    out = escape_and_check(["Shipped 10 features"])
    assert out == ["Shipped 10 features"]


def test_multiple_bullets_processed_independently():
    out = escape_and_check([
        "Plain bullet",
        "50% saved",
        r"Used \textbf{Python}",
    ])
    assert len(out) == 3
    assert out[0] == "Plain bullet"
    assert r"\%" in out[1]
    assert r"\textbf" in out[2]


def test_denylist_aborts_before_processing_other_bullets():
    with pytest.raises(SanitizerReject):
        escape_and_check([
            "Safe first bullet",
            r"\write18{evil}",
            "Safe third bullet",
        ])
