"""HTTP API layer for the phase diagram service."""
from __future__ import annotations

__all__ = ["app"]


def __getattr__(name: str):
    if name == "app":
        from .app import app as fastapi_app

        return fastapi_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
