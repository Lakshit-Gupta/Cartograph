"""/scrape-status slash — per-source pipeline health snapshot.

Returns one line per active source showing:
  - last successful crawl timestamp
  - total opps captured
  - opps captured in the last 24h
  - count of opps with a score above the (auto_apply) per-source min_score

Used to answer "is the scraper alive + is anything making it through the
filter chain to the auto-apply candidate pool?" without SSHing the Pi.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import discord
import yaml

from src.common.db import fetch_all
from src.common.logger import get_logger
from src.common.secrets import get_settings

_log = get_logger(__name__)


def _load_min_score_map() -> tuple[float, dict[str, float]]:
    """Returns (global_min_score, per_source_overrides)."""
    settings = get_settings()
    path = Path(settings.config_root) / "profile" / "prefs.yaml"
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        _log.warning("scrape_status_prefs_read_failed", err=str(e))
        return 0.30, {}
    aa = loaded.get("auto_apply") or {}
    global_min = float(aa.get("min_score", 0.30))
    per_src = aa.get("per_source_min_score") or {}
    if not isinstance(per_src, dict):
        per_src = {}
    return global_min, {k: float(v) for k, v in per_src.items()}


def setup(bot) -> None:  # type: ignore[no-untyped-def]
    @bot.tree.command(name="scrape-status", description="Per-source scrape pipeline health.")
    async def scrape_status(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        global_min, per_src = _load_min_score_map()
        # Materialize per-source min_score as a SQL CASE so the
        # `above_threshold` count uses the correct cutoff per source.
        if per_src:
            cases = " ".join(f"WHEN s.slug = '{slug}' THEN {float(v)}" for slug, v in per_src.items())
            threshold_expr = f"(CASE {cases} ELSE {global_min} END)"
        else:
            threshold_expr = f"{global_min}"

        sql = f"""
        SELECT s.slug,
               s.last_successful_crawl_at AS last_crawl,
               COUNT(o.id) AS opp_count,
               COUNT(o.id) FILTER (WHERE o.first_seen > NOW() - INTERVAL '24 hours') AS last_24h,
               COUNT(o.id) FILTER (WHERE os.score >= {threshold_expr}) AS above_threshold
        FROM sources s
        LEFT JOIN opportunities o ON o.source_id = s.id
        LEFT JOIN opportunity_scores os ON os.opportunity_id = o.id AND os.user_id = 1
        WHERE s.status = 'active'
        GROUP BY s.slug, s.last_successful_crawl_at
        ORDER BY last_24h DESC NULLS LAST, opp_count DESC
        """
        try:
            rows = await fetch_all(sql)
        except Exception as e:
            _log.exception("scrape_status_query_failed", err=str(e))
            await interaction.followup.send(f"Error: {e}", ephemeral=True)
            return

        if not rows:
            await interaction.followup.send("No active sources.", ephemeral=True)
            return

        lines: list[str] = []
        for r in rows:
            last_crawl = r["last_crawl"]
            crawl_age = f"<t:{int(last_crawl.timestamp())}:R>" if last_crawl is not None else "never"
            lines.append(
                f"`{r['slug']:<24}` "
                f"opps={int(r['opp_count']):>5} "
                f"24h={int(r['last_24h']):>3} "
                f"≥thr={int(r['above_threshold']):>3} "
                f"crawl={crawl_age}"
            )

        body = "\n".join(lines)
        # Discord embed field cap is 1024 chars; chunk if needed.
        embed = discord.Embed(
            title="Scrape pipeline status",
            color=0x2ECC71,
        )
        # Split into chunks of ~1000 chars to stay under embed field limit.
        chunks: list[str] = []
        current = ""
        for line in body.split("\n"):
            if len(current) + len(line) + 1 > 1000:
                chunks.append(current)
                current = line
            else:
                current = f"{current}\n{line}" if current else line
        if current:
            chunks.append(current)
        for i, chunk in enumerate(chunks):
            embed.add_field(
                name=f"Sources ({i + 1}/{len(chunks)})" if len(chunks) > 1 else "Sources",
                value=f"```{chunk}```",
                inline=False,
            )

        _ = Any  # silence unused-import if Any wasn't used elsewhere
        await interaction.followup.send(embed=embed, ephemeral=True)
