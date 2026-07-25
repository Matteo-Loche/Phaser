"""Server usage statistics endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Query

from ...db.stats_store import DEFAULT_STATS_WINDOW, normalize_stats_window
from ...services.stats import get_summary

router = APIRouter(tags=["stats"])


@router.get("/api/stats")
def stats_summary(
    window: str = Query(
        DEFAULT_STATS_WINDOW,
        description="Trailing window: 24h, 7d, 30d, 90d, 1y, or all",
    ),
):
    return get_summary(window=normalize_stats_window(window))
