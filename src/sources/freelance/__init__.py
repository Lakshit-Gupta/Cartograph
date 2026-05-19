"""Freelance speed lane source plugins.

The central plugin registry (`src/sources/registry.py`) eagerly imports
this subpackage to trigger `@register(PLUGIN)` side effects on each
freelance plugin module. The Phase 3.3 bounty lane plugins live in
`bounty_*.py` siblings that are not enumerated in `registry.py` (per
the no-modify constraint) — instead we import them here so they
self-register the moment `src.sources.freelance` itself is imported.

If you add a new freelance plugin file, add a side-effect import here
to keep it discoverable from the running scheduler / extractor_worker.
"""

from __future__ import annotations

# Side-effect imports — each module's top-level `register(PLUGIN)` call
# fires when the module loads. The `# noqa: F401` suppresses unused-import
# warnings. Ordering is alphabetic for grep-ability.
from . import (  # noqa: F401
    bounty_algora,
    bounty_gitcoin,
    bounty_replit,
    bounty_superteam,
)
