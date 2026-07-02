"""Database registry endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ... import config
from ...db.catalog_store import catalog_public_meta
from ...db.registry import (
    get_database,
    get_default_database,
    is_database_disabled,
    list_enabled_databases,
    register_generated_database,
)
from ...services.catalog import scan_database_record
from ..models import RegisterDatabaseRequest

router = APIRouter(tags=["databases"])


def _public_record(rec) -> dict:
    out = rec.public_dict()
    out.update(catalog_public_meta(rec))
    return out


@router.get("/api/databases")
def api_list_databases():
    records = list_enabled_databases()
    default_id = None
    if any(r.exists for r in records):
        try:
            default_id = get_default_database().id
        except RuntimeError:
            default_id = None
    return {
        "databases": [_public_record(rec) for rec in records],
        "count": len(records),
        "default_db_id": default_id,
    }


@router.get("/api/databases/{db_id}")
def api_get_database(db_id: str):
    rec = get_database(db_id)
    if not rec or is_database_disabled(rec):
        raise HTTPException(404, f"Database id not found: {db_id}")
    return _public_record(rec)


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
        scan_database_record(rec)
    except (FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    meta = _public_record(rec)
    if meta.get("catalog_status") == "failed":
        raise HTTPException(422, meta.get("catalog_error") or "catalog scan failed")
    return {"registered": True, "database": meta}
