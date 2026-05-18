"""LaTeX resume subsystem — parse, tailor, render, compile.

Public surface kept minimal; callers should use:
  - parser.manifest.load(path)
  - parser.blocks.parse(manifest, root)
  - selector.rank(blocks, opp, variant_keywords)
  - sanitizer.escape_and_check(bullets)
  - render.write_partial(doc, edits, artifact_dir) / commit_complete(partial)
  - compile.run(main_tex)
  - plaintext.to_plain_text(latex)
  - fallback.warm_fallback_pdf(user_id, manifest_path) / get_fallback(user_id)

See CLAUDE.md "LaTeX resume subsystem" for the ratified design.
"""
