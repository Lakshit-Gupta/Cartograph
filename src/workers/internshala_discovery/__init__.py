"""Internshala browser-discovery worker helper package.

The entrypoint lives at `src.workers.internshala_discovery_worker`; this package
holds the supporting modules kept split so no single file exceeds the repo's
300-line ceiling:

  - `config`  — env + YAML config loading, RECON_PENDING guard, combo expansion.
  - `report`  — `DiscoveryCycleReport` dataclass + pure floor/dedup/payload helpers.
  - `cycle`   — `run_cycle` / `run_combo` browser-driven scrape orchestration.
"""

from __future__ import annotations
