"""LaTeX apply pipeline — orchestrator + tailor + compile + audit + fallback.

Public entry point: :func:`send_with_latex`. Internal submodules:

- :mod:`pipeline` - the orchestrator.
- :mod:`tailor` - cost-gated LLM bullet rewriting.
- :mod:`compile_pipeline` - render + tectonic + post-process + promote.
- :mod:`audit` - ``resume_compile_log`` row builder.
- :mod:`fallback_path` - fallback PDF resolution.

CLAUDE.md hard rules preserved:

1. Sanitiser ALWAYS runs between LLM and render (`pipeline._sanitize_edits`).
2. ``tectonic --untrusted`` + 30 s timeout + ``kill_group=True``
   (delegated to ``src.application.resume_latex.compile.run``).
3. PDF metadata scrubbed (``exiftool -all:all=`` inside ``compile.run``).
4. PDF NEVER posted to Discord — email attachment only
   (`pipeline._publish_notify`).
5. Source-hash drift guard intact (re-checked inside
   ``resume_latex.render.write_partial``).
6. ``applications`` UPSERT runs through current ``user_id`` resolved by
   the caller (``sender.send_application``).
7. ``applications.resume_compile_status`` written on every success path.
8. LLM cost ledger ``kind="llm_writer"`` (V001-compatible enum).
"""

from .pipeline import send_with_latex

__all__ = ["send_with_latex"]
