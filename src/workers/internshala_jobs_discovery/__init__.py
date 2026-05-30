"""Internshala JOBS discovery worker (full-time roles, 12 LPA strict-min floor).

Sibling of `src.workers.internshala_discovery` (internships). Reuses the shared,
source-agnostic helpers (browser ops, the cycle-report dataclass + validity gate,
the persist write-path, the salary/date/experience parsers) by import; only the
jobs-specific URL building, config, card parser, and the strict-min salary +
experience gates live here. See
docs/superpowers/specs (Internshala jobs discovery) + the plan file.
"""
