"""Persisted UI state endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ...services.state import load_saved_state, save_saved_state

router = APIRouter(tags=["state"])


@router.get("/api/state")
def load_state():
    return load_saved_state()


@router.post("/api/state")
def save_state(body: dict[str, Any]):
    return save_saved_state(body)
