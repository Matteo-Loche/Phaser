"""Server usage statistics endpoints."""
from __future__ import annotations

from fastapi import APIRouter

from ...services.stats import get_summary

router = APIRouter(tags=["stats"])


@router.get("/api/stats")
def stats_summary():
    return get_summary()
