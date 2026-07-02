"""Shared FastAPI dependencies and resolvers."""
from __future__ import annotations

from fastapi import HTTPException

from ..db.registry import DatabaseRecord, resolve_database


def resolve_db_record(db_id: str | None = None, db_path: str | None = None) -> DatabaseRecord:
    try:
        return resolve_database(db_id=db_id, db_path=db_path)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
