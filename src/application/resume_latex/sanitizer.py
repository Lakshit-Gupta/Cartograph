"""LaTeX sanitizer — defense between the LLM and the renderer.

Hard rule from CLAUDE.md "LaTeX resume subsystem":
    Never splice raw LLM output. `sanitizer.escape_and_check` runs between
    LLM and `render`. Reject any LLM bullet containing a `\\command` outside
    the allowlist (`\\textbf`, `\\textit`, `\\emph`, escaped specials).

Two layers of defense:
1. *Denylist* — reject the whole bullet if it contains any forbidden macro
   that could break out of the resume tree (file I/O, code execution,
   catcode rewrites, etc). Raises `SanitizerReject`.
2. *Allowlist + escape* — strip any backslash command not on the allowlist
   (silently — denylist already caught the dangerous ones), then escape
   LaTeX specials (& % $ # _ { } ~ ^ \\).

The sanitizer is intentionally chatty: every reject raises with the macro
name so the caller can log + log + fall back without guessing the cause.
"""
from __future__ import annotations

import re

# Macros that NEVER appear in a clean bullet. Their presence in LLM output
# means either (a) the LLM is being prompt-injected from the opp text, or
# (b) the model is hallucinating LaTeX. Either way: reject the whole bullet.
_DENY: tuple[str, ...] = (
    r"\write18",
    r"\input",
    r"\openin",
    r"\openout",
    r"\read",
    r"\catcode",
    r"\immediate",
    r"\directlua",
    r"\loop",
    r"\csname",
    r"\def",
    r"\xdef",
    r"\let",
    r"\expandafter",
)

# The only formatting macros the LLM is allowed to leave intact. Everything
# else (e.g. \section, \href, \customMacro) is stripped.
_ALLOW: frozenset[str] = frozenset({r"\textbf", r"\textit", r"\emph"})

# Word-boundary matchers for denylist entries. ``\b`` won't work after a
# backslash, so we anchor with a negative lookahead for [A-Za-z]: each
# command must end at a non-letter character.
_DENY_RE = re.compile(
    "|".join(re.escape(d) + r"(?![A-Za-z])" for d in _DENY),
    flags=0,
)

# Any backslash command (\word) — used to strip non-allowed wrappers.
_CMD_RE = re.compile(r"\\[A-Za-z]+")

# LaTeX specials that must be escaped. Order matters: backslash first so
# we don't double-escape the escapes we ourselves insert. We pre-process
# the string instead of using str.translate because some replacements are
# multi-character (e.g. ``~`` → ``\textasciitilde{}``).
_ESCAPES: tuple[tuple[str, str], ...] = (
    ("\\", r"\textbackslash{}"),
    ("&", r"\&"),
    ("%", r"\%"),
    ("$", r"\$"),
    ("#", r"\#"),
    ("_", r"\_"),
    ("{", r"\{"),
    ("}", r"\}"),
    ("~", r"\textasciitilde{}"),
    ("^", r"\textasciicircum{}"),
)


class SanitizerReject(ValueError):
    """The LLM output contained a forbidden macro and must not be spliced."""


def _strip_unallowed_commands(text: str) -> str:
    """Remove any \\cmd that is not on the allowlist.

    The allowlisted commands are *kept* — only their arguments survive
    because the subsequent escape step replaces the leading backslash with
    \\textbackslash. So we have to keep the allowlisted commands by
    temporarily swapping them out, escape, then swap them back.
    """
    # Strip non-allowed commands first.
    def _strip(m: re.Match[str]) -> str:
        return "" if m.group(0) not in _ALLOW else m.group(0)
    return _CMD_RE.sub(_strip, text)


def _escape_specials(text: str) -> str:
    """Escape LaTeX specials, preserving the allowlisted formatting macros."""
    # 1. Temporarily replace allowlisted commands with sentinel tokens so
    #    the backslash-escape pass doesn't mangle them.
    sentinels: list[tuple[str, str]] = []
    for i, allowed in enumerate(sorted(_ALLOW, key=len, reverse=True)):
        marker = f"\x00ALLOW{i}\x00"
        if allowed in text:
            text = text.replace(allowed, marker)
            sentinels.append((marker, allowed))

    # 2. Escape specials. We process the longer replacements first via the
    #    fixed _ESCAPES ordering (backslash → first).
    for ch, repl in _ESCAPES:
        text = text.replace(ch, repl)

    # 3. Restore the allowlisted commands. Their escape-table substitutions
    #    inside the sentinel-protected ranges never happened.
    for marker, allowed in sentinels:
        text = text.replace(marker, allowed)
    return text


def escape_and_check(bullets: list[str]) -> list[str]:
    """Return a sanitised copy of ``bullets``.

    Raises:
        SanitizerReject: if any bullet contains a denylisted macro.
    """
    out: list[str] = []
    for b in bullets:
        m = _DENY_RE.search(b)
        if m:
            raise SanitizerReject(f"forbidden macro detected: {m.group(0)}")
        cleaned = _strip_unallowed_commands(b)
        cleaned = _escape_specials(cleaned)
        out.append(cleaned)
    return out
