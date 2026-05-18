"""Block walker — turn a pylatexenc node tree into tailorable Blocks.

A Block represents one tailorable region of the resume tree. Each block has
a stable id (sha256 of kind|title|bullets), a kind (matched against the
manifest's macro_vocabulary), a plain-text title, the bullet list pulled
from the *next* itemize environment, and the char range of the originating
macro inside its source file.

Source-hash drift guard: ``Document.source_hashes`` is recorded at parse
time. ``render.py`` re-reads the file and aborts if the hash drifted.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from pylatexenc.latexwalker import (  # type: ignore[import-untyped]
    LatexCharsNode,
    LatexEnvironmentNode,
    LatexGroupNode,
    LatexMacroNode,
)

from src.application.resume_latex.parser.lexer import tokenise
from src.application.resume_latex.parser.manifest import ResumeManifest


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Block:
    """A single tailorable region of the resume tree.

    Attributes:
        id: sha256(kind|title|bullets), first 32 hex chars. Used as an
            opaque handle by the LLM tailoring step.
        kind: the manifest key whose macro_vocabulary list contained the
            macroname (e.g. "event", "section", "project").
        title: first plain-text mandatory arg of the macro.
        bullets: plain-text items pulled from the *following* itemize env.
            Empty when the macro is not followed by an itemize block.
        file: filename of the source .tex (e.g. mmayer.tex).
        char_range: (start, end) byte offset of the **macro + its trailing
            itemize block** inside the source. Renderer re-splices this
            range with the new bullets, preserving everything outside it.
    """

    id: str
    kind: str
    title: str
    bullets: list[str]
    file: str
    char_range: tuple[int, int]


@dataclass(frozen=True)
class Document:
    """Result of parsing the resume tree.

    Attributes:
        blocks: every tailorable Block discovered across every file in
            ``files``. Order is parse order (top-to-bottom across files).
        files: filename -> raw source string. The renderer mutates copies
            of these strings.
        source_hashes: filename -> sha256(source). Drift guard: render
            re-reads from disk and aborts if the hash changed under us.
    """

    blocks: list[Block]
    files: dict[str, str]
    source_hashes: dict[str, str]
    manifest: ResumeManifest = field(default=None)  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Argument-text extraction
# ---------------------------------------------------------------------------
def _arg_to_text(arg: object) -> str:
    """Walk one macro argument, returning plain text.

    Handles the common AltaCV wrappers: ``\\textbf{...}``, ``\\textit{...}``,
    ``\\emph{...}``, and ``\\href{url}{visible}`` (we take the LAST mandatory
    arg, which is the visible text). We deliberately do NOT use
    ``LatexNodes2Text`` here because its default ``\\href`` mapper crashes
    on the AltaCV ``\\linkedin{\\href{url}{Linkedin}}`` shape.
    """
    if arg is None:
        return ""
    if isinstance(arg, LatexGroupNode) or hasattr(arg, "nodelist"):
        nodes = list(arg.nodelist)
    else:
        nodes = [arg]

    parts: list[str] = []
    for n in nodes:
        if isinstance(n, LatexCharsNode):
            parts.append(n.chars)
        elif isinstance(n, LatexGroupNode):
            parts.append(_arg_to_text(n))
        elif isinstance(n, LatexMacroNode):
            argd = getattr(n, "nodeargd", None)
            if argd is not None and getattr(argd, "argnlist", None):
                # Visible text usually lives in the last mandatory arg.
                parts.append(_arg_to_text(argd.argnlist[-1]))
        # Comments, math, environments inside an arg: ignore.
    return _NORM_WS.sub(" ", "".join(parts)).strip()


_NORM_WS = re.compile(r"\s+")
_TRAILING_PUNCT = re.compile(r"[\s\.;,\\]+$")


# ---------------------------------------------------------------------------
# Bullets extraction
# ---------------------------------------------------------------------------
def _bullets_from_itemize(env: LatexEnvironmentNode) -> list[str]:
    """Return one string per ``\\item`` in an itemize environment.

    Nested itemize (skills lists like ``Programming Languages → C++, Java``)
    is flattened: each inner ``\\item`` becomes its own bullet.
    """
    out: list[str] = []
    pending: list[str] = []

    def flush() -> None:
        if pending:
            text = _NORM_WS.sub(" ", "".join(pending)).strip()
            text = _TRAILING_PUNCT.sub("", text)
            if text:
                out.append(text)
            pending.clear()

    for n in env.nodelist:
        if isinstance(n, LatexMacroNode) and n.macroname == "item":
            flush()
            continue
        if isinstance(n, LatexCharsNode):
            pending.append(n.chars)
        elif isinstance(n, LatexGroupNode):
            pending.append(_arg_to_text(n))
        elif isinstance(n, LatexMacroNode):
            argd = getattr(n, "nodeargd", None)
            if argd is not None and getattr(argd, "argnlist", None):
                pending.append(_arg_to_text(argd.argnlist[-1]))
        elif isinstance(n, LatexEnvironmentNode) and n.environmentname == "itemize":
            # Nested list: recurse, append each child bullet as its own line.
            flush()
            out.extend(_bullets_from_itemize(n))
    flush()
    return out


# ---------------------------------------------------------------------------
# Vocabulary lookup
# ---------------------------------------------------------------------------
def _kind_for_macro(name: str, vocabulary: dict[str, list[str]]) -> str | None:
    """Return the first kind whose list contains ``name`` (or None)."""
    for kind, names in vocabulary.items():
        if name in names:
            return kind
    return None


# ---------------------------------------------------------------------------
# Public parse()
# ---------------------------------------------------------------------------
def parse(manifest: ResumeManifest, root: Path) -> Document:
    """Walk every file the resume actually compiles, returning a Document.

    Files included:
        - manifest.main_file
        - any `\\input{...}` referenced from main_file
        - any sidebar referenced via `\\cvsection[<filename>]{...}` (AltaCV
          loads `<filename>.tex` as the sidebar for that section)
    """
    files: dict[str, str] = {}
    hashes: dict[str, str] = {}
    blocks: list[Block] = []

    vocabulary = manifest.macro_vocabulary
    # Flat union of all macros the parser needs to know about. Passed to the
    # lexer so it can register their arg signatures.
    voc_flat = sorted({m for names in vocabulary.values() for m in names})

    discovered = [manifest.main_file]
    seen: set[str] = set()
    while discovered:
        fname = discovered.pop(0)
        if fname in seen:
            continue
        seen.add(fname)
        path = (root / fname)
        if not path.exists():
            continue
        src = path.read_text(encoding="utf-8")
        files[fname] = src
        hashes[fname] = hashlib.sha256(src.encode("utf-8")).hexdigest()

        nodes = tokenise(src, voc_flat)
        # Walk discovering blocks + dependent files
        file_blocks, more_files = _walk_file(nodes, src, fname, manifest)
        blocks.extend(file_blocks)
        for extra in more_files:
            if extra not in seen:
                discovered.append(extra)

    return Document(blocks=blocks, files=files, source_hashes=hashes, manifest=manifest)


def _walk_file(
    nodes: list,
    src: str,
    fname: str,
    manifest: ResumeManifest,
) -> tuple[list[Block], list[str]]:
    """Inner walker — returns (blocks_in_this_file, dependent_filenames)."""
    out_blocks: list[Block] = []
    deps: list[str] = []
    vocabulary = manifest.macro_vocabulary
    exclude_set = {s.strip().lower() for s in manifest.exclude_sections}

    current_section_title: str = ""  # tracks the most recent \cvsection title
    pending_macro: tuple[LatexMacroNode, str, str] | None = None
    # (macro_node, kind, title) — we hold onto the macro until we see
    # either (a) its following itemize env or (b) the next macro / EOF.

    def commit_block(macro: LatexMacroNode, kind: str, title: str,
                     bullets: list[str], end_pos: int) -> None:
        if current_section_title.strip().lower() in exclude_set:
            return
        block_id_seed = f"{kind}|{title}|" + "|".join(bullets)
        block_id = hashlib.sha256(block_id_seed.encode("utf-8")).hexdigest()[:32]
        out_blocks.append(Block(
            id=block_id,
            kind=kind,
            title=title,
            bullets=bullets,
            file=fname,
            char_range=(macro.pos, end_pos),
        ))

    for n in nodes:
        # ----------------- itemize: belongs to pending_macro -----------------
        if isinstance(n, LatexEnvironmentNode) and n.environmentname == "itemize":
            if pending_macro is not None:
                macro, kind, title = pending_macro
                bullets = _bullets_from_itemize(n)
                commit_block(macro, kind, title, bullets, n.pos + n.len)
                pending_macro = None
            continue

        # ----------------- macro: handle by kind -----------------
        if isinstance(n, LatexMacroNode):
            kind = _kind_for_macro(n.macroname, vocabulary)
            argd = getattr(n, "nodeargd", None)
            argnlist = (argd.argnlist if argd is not None else []) or []

            # \input / \include: discover sidecar files
            if n.macroname in ("input", "include") and argnlist:
                ref = _arg_to_text(argnlist[0])
                if ref:
                    deps.append(ref if ref.endswith(".tex") else f"{ref}.tex")
                continue

            # \cvsection — special: optional first arg is a sidebar file ref;
            # second mandatory arg is the section title (tracked for exclusion).
            if n.macroname == "cvsection":
                # Flush a prior pending macro that had no itemize (e.g. a
                # \cvevent followed by another \cvevent — emit the first
                # with an empty bullet list so it is still addressable).
                if pending_macro is not None:
                    macro, p_kind, p_title = pending_macro
                    commit_block(macro, p_kind, p_title, [], macro.pos + macro.len)
                    pending_macro = None

                if argnlist:
                    sidebar_arg = argnlist[0] if len(argnlist) >= 2 else None
                    title_arg = argnlist[-1]
                    sidebar = _arg_to_text(sidebar_arg) if sidebar_arg else ""
                    title = _arg_to_text(title_arg)
                    current_section_title = title
                    if sidebar:
                        deps.append(sidebar if sidebar.endswith(".tex") else f"{sidebar}.tex")
                    # Treat as a "section" Block even without bullets — the
                    # following itemize (if any) will overwrite via pending.
                    if kind is not None:
                        pending_macro = (n, kind, title)
                continue

            # Generic tailorable macro
            if kind is None:
                continue

            # If we had a pending macro without an itemize, commit it empty.
            if pending_macro is not None:
                macro, p_kind, p_title = pending_macro
                commit_block(macro, p_kind, p_title, [], macro.pos + macro.len)
                pending_macro = None

            title = _arg_to_text(argnlist[0]) if argnlist else ""
            pending_macro = (n, kind, title)
            continue

    # Flush trailing pending macro at end-of-file
    if pending_macro is not None:
        macro, kind, title = pending_macro
        commit_block(macro, kind, title, [], macro.pos + macro.len)

    return out_blocks, deps
