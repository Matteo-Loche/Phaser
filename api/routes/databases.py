"""Database registry endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ... import config
from ...db.registry import (
    get_database,
    get_default_database,
    list_databases,
    register_generated_database,
)
from ..models import RegisterDatabaseRequest

router = APIRouter(tags=["databases"])


@router.get("/api/databases")
def api_list_databases():
    records = list_databases()
    default_id = None
    if any(r.exists for r in records):
        try:
            default_id = get_default_database().id
        except RuntimeError:
            default_id = None
    return {
        "databases": [rec.public_dict() for rec in records],
        "count": len(records),
        "default_db_id": default_id,
    }


@router.get("/api/databases/{db_id}")
def api_get_database(db_id: str):
    rec = get_database(db_id)
    if not rec:
        raise HTTPException(404, f"Database id not found: {db_id}")
    return rec.public_dict()


@router.post("/api/databases/register")
def api_register_database(body: RegisterDatabaseRequest):
    """Register or refresh metadata for a generated database on the server."""
    dat_path = config.GENERATED_DB_DIR / body.filename
    if not dat_path.is_file():
        raise HTTPException(
            404,
            f"Generated database file not found on server: {body.filename}. "
            f"Place the .dat file in {config.GENERATED_DB_DIR} first.",
        )
    try:
        rec = register_generated_database(dat_path, metadata=body.metadata)
    except (FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"registered": True, "database": rec.public_dict()}
