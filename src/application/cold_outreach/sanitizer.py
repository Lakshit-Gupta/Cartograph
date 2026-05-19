"""Text sanitizer for Apollo / Hunter responses + LLM drafts.

The cold-outreach LLM is fed UNTRUSTED data: recipient bios and titles
from Apollo, mission summaries from the user's curated `target_companies`
rows (the user types these, but they're still text the LLM will splice
into its prompt context). Two defenses:

1. `scrub_text` — strip HTML tags, normalise whitespace, drop ASCII
   control chars, clamp length. Used on every Apollo / Hunter field
   that flows into the drafter prompt.
2. `subject_hash` — SHA-256 of the *normalised* subject. Used by
   cap.py to enforce the 30-day subject-dedupe rule. The hash is
   case-insensitive and whitespace-tolerant so trivial variants
   collide as intended.

This module is intentionally separate from
`src/application/resume_latex/sanitizer.py` because that one defends
against LaTeX macro injection in tectonic input. The two share no
machinery.
"""

from __future__ import annotations

import hashlib
import re

# Match any HTML/XML tag, including self-closing and namespaced (<o:p>, <a/>).
_TAG_RE = re.compile(r"<[^>]*>")

# ASCII control chars and the Unicode BOM — strip silently.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f﻿]")

# Collapse runs of whitespace (including non-breaking spaces) to single spaces.
_WHITESPACE_RE = re.compile(r"\s+")


def scrub_text(s: str | None, *, max_len: int = 500) -> str:
    """Return a safe single-line version of `s`.

    - Strips HTML/XML tags.
    - Drops control characters.
    - Collapses whitespace.
    - Truncates to `max_len`.

    Returns "" when input is None / falsy after stripping.
    """
    if not s:
        return ""
    out = _TAG_RE.sub(" ", s)
    out = _CONTROL_RE.sub("", out)
    out = _WHITESPACE_RE.sub(" ", out).strip()
    if len(out) > max_len:
        out = out[:max_len].rstrip()
    return out


def subject_hash(subject: str) -> str:
    """Stable hash of the *normalised* subject.

    Lowercase + collapse whitespace before hashing so trivial variants
    (`"Quick intro"` vs `" Quick   Intro "`) collide. We deliberately do
    NOT strip punctuation — the user may legitimately want two distinct
    subjects that differ only in `!` placement.
    """
    norm = _WHITESPACE_RE.sub(" ", subject.strip().lower())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def word_count(body: str) -> int:
    """Naive whitespace-delimited word count used to enforce the 90-word cap."""
    return len([w for w in body.split() if w.strip()])
