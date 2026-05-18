"""Pylatexenc walker wrapper.

The wrapper exists for two reasons:
1. Pylatexenc does not know about AltaCV custom macros (\\cvsection,
   \\cvevent, \\cvproject). Without a registered MacroSpec the macro's
   argument list is invisible and `len`/`pos` cover only the macro name.
2. The walker must traverse into the document environment — top-level
   nodes are class-level (\\documentclass, \\geometry, ...). All
   tailorable content lives inside ``\\begin{document}...\\end{document}``.

`tokenise(source, vocabulary)` returns the *document-body* node list with
custom macros registered. Callers iterate the list and match
``LatexMacroNode.macroname`` against their own dispatch dict.
"""
from __future__ import annotations

from collections.abc import Iterable

from pylatexenc.latexwalker import (  # type: ignore[import-untyped]
    LatexEnvironmentNode,
    LatexWalker,
    get_default_latex_context_db,
)
from pylatexenc.macrospec import MacroSpec  # type: ignore[import-untyped]


# Argument signatures for the AltaCV macros we care about.
#  - '[' = optional bracketed arg (e.g. \\cvsection[page1sidebar]{Title})
#  - '{' = mandatory braced arg
#
# When a macroname appears in user vocabulary but not in this map, it falls
# back to a single mandatory arg ('{') — safe default that still walks past
# the call without consuming siblings.
_KNOWN_SIGS: dict[str, str] = {
    "cvsection":   "[{",
    "cvevent":     "{{{{",
    "cvproject":   "{",
    "name":        "{",
    "tagline":     "{",
    "personalinfo": "{",
    "input":       "{",
    "include":     "{",
}


def _build_context(custom_macros: Iterable[str]) -> object:
    """Return a fresh latex-context db with the requested custom macros."""
    db = get_default_latex_context_db()
    specs = []
    for name in custom_macros:
        sig = _KNOWN_SIGS.get(name, "{")
        specs.append(MacroSpec(name, sig))
    db.add_context_category("manifest_macros", macros=specs, environments=[])
    return db


def tokenise(source: str, vocabulary: Iterable[str] | None = None) -> list:
    """Return the node list of ``\\begin{document}...\\end{document}``.

    Falls back to the top-level node list if no ``document`` environment is
    present (e.g. sidebar `.tex` files that get pulled in via ``\\cvsection``
    optional arg — those have no \\begin{document} wrapper).

    Args:
        source: the raw .tex string.
        vocabulary: iterable of macro names whose argument signature should
            be registered before walking. Defaults to the AltaCV defaults.
    """
    voc = list(vocabulary) if vocabulary is not None else list(_KNOWN_SIGS.keys())
    db = _build_context(voc)
    walker = LatexWalker(source, latex_context=db)
    nodes, _, _ = walker.get_latex_nodes()
    for n in nodes:
        if isinstance(n, LatexEnvironmentNode) and n.environmentname == "document":
            return list(n.nodelist)
    return list(nodes)
