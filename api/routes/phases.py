"""Phase discovery endpoint."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ...db.catalog_store import list_phases, require_ready
from ..dependencies import resolve_db_record
from ..models import PhaseQuery

router = APIRouter(tags=["phases"])


@router.post("/api/phases")
def api_phases(body: PhaseQuery):
    rec = resolve_db_record(db_id=body.db_id, db_path=body.db_path)
    try:
        db_key = require_ready(rec)
        phases = list_phases(
            db_key,
            system_elements=set(body.elements),
            selected=set(body.selected) if body.selected else None,
            exclude_gases=body.exclude_gases,
        )
    except LookupError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {
        "phases": phases,
        "count": len(phases),
    }
