"""Feedback loop — rolls per-source / per-lane response rate from applications.

A simple rolling response rate (response within 14 days / total apps) per source
becomes the `response_rate` input to formula.score().
"""

from __future__ import annotations

from src.common.db import acquire


async def source_response_rates(window_days: int = 60) -> dict[int, float]:
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            WITH apps AS (
                SELECT o.source_id,
                       COUNT(*)                                        AS sent,
                       COUNT(*) FILTER (
                         WHERE a.response_status IS NOT NULL
                           AND a.response_at IS NOT NULL
                           AND a.response_at <= a.sent_at + INTERVAL '14 days'
                       ) AS responded
                FROM applications a
                JOIN opportunities o ON o.id = a.opportunity_id
                WHERE a.sent_at >= NOW() - ($1::int || ' days')::interval
                GROUP BY o.source_id
            )
            SELECT source_id,
                   CASE WHEN sent = 0 THEN 0 ELSE responded::float / sent END AS rate
            FROM apps
            """,
            window_days,
        )
    return {int(r["source_id"]): float(r["rate"]) for r in rows}


async def lane_response_rates(window_days: int = 60) -> dict[str, float]:
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            WITH apps AS (
                SELECT o.category,
                       COUNT(*)                                        AS sent,
                       COUNT(*) FILTER (
                         WHERE a.response_status IS NOT NULL
                       ) AS responded
                FROM applications a
                JOIN opportunities o ON o.id = a.opportunity_id
                WHERE a.sent_at >= NOW() - ($1::int || ' days')::interval
                GROUP BY o.category
            )
            SELECT category,
                   CASE WHEN sent = 0 THEN 0 ELSE responded::float / sent END AS rate
            FROM apps
            """,
            window_days,
        )
    return {str(r["category"]): float(r["rate"]) for r in rows}
