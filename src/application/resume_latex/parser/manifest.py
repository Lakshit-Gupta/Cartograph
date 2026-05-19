"""ResumeManifest — Pydantic loader for config/profile/my_resume/manifest.yaml.

The manifest drives the entire LaTeX subsystem: which file is the entry point,
which macros are tailorable, which sections must be skipped, and what PDF
metadata to scrub into the compiled output.

See CLAUDE.md "LaTeX resume subsystem" hard rule #8: the macro vocabulary
lives in the manifest, never hardcoded. That is what makes a class swap
(moderncv, Awesome-CV) a config edit rather than a code edit.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class ResumeManifest(BaseModel):
    """Validated view of `manifest.yaml`.

    Fields:
        main_file: filename of the .tex compiled by tectonic (e.g. mmayer.tex).
        class_file: AltaCV style sheet — informational; never modified.
        macro_vocabulary: ``kind -> [macro_names]``. Parser emits one Block
            per macro occurrence whose macroname appears in the list for that
            kind. The kind is the dict key (e.g. "event", "section", "project").
        exclude_sections: titles of \\cvsection blocks whose contents must
            never be tailored. Selector enforces this.
        output_name: filename of the compiled PDF (default ``resume.pdf``).
        pdf_metadata: scrubbed into the PDF post-compile via exiftool and
            injected into \\hypersetup at render time (when the source allows).
    """

    main_file: str
    class_file: str
    macro_vocabulary: dict[str, list[str]]
    exclude_sections: list[str] = Field(default_factory=list)
    output_name: str = "resume.pdf"
    pdf_metadata: dict[str, str] = Field(default_factory=dict)
    # Phase 2.2 — optional A/B variants. Maps a variant label
    # (``backend``/``fullstack``/``ml``/``freelance``/``intern_india``) to the
    # relative path of its own main .tex. When unset (legacy single-variant
    # config), the apply pipeline falls back to ``main_file`` and treats the
    # variant id as 1 in the DB.
    variants: dict[str, str] = Field(default_factory=dict)


def load(path: Path) -> ResumeManifest:
    """Load and validate a ``manifest.yaml`` from ``path``.

    Also resolves variant paths relative to ``path.parent``. A configured
    variant whose file is missing on disk is dropped from the dict so the
    runtime picker never selects a label whose .tex doesn't exist — bad
    YAML keeps Phase 1 single-variant working instead of bricking the
    pipeline.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"manifest must be a mapping, got {type(raw).__name__}: {path}")
    manifest = ResumeManifest(**raw)
    if manifest.variants:
        root = path.parent
        manifest.variants = {label: rel for label, rel in manifest.variants.items() if (root / rel).is_file()}
    return manifest
