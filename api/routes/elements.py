"""Element listing endpoint."""
from __future__ import annotations

from fastapi import APIRouter

from ...db.parser import list_elements
from ...services.species import species_for_database
from ..dependencies import resolve_db_record

router = APIRouter(tags=["elements"])


@router.get("/api/elements")
def api_elements(db_id: str | None = None, db_path: str | None = None):
    rec = resolve_db_record(db_id=db_id, db_path=db_path)
    elems = list_elements(rec.path)
    return {
        "elements": elems,
        "count": len(elems),
        "db_id": rec.id,
        "db_name": rec.name,
        "species_suggestions": species_for_database(elems),
    }
