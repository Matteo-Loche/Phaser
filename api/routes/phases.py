"""Phase discovery endpoint."""
from __future__ import annotations

from fastapi import APIRouter

from ...db.parser import filter_phases
from ..dependencies import resolve_db_record
from ..models import PhaseQuery

router = APIRouter(tags=["phases"])


@router.post("/api/phases")
def api_phases(body: PhaseQuery):
    rec = resolve_db_record(db_id=body.db_id, db_path=body.db_path)
    path = rec.path
    elems = set(body.elements)
    selected = set(body.selected) if body.selected else None
    phases = filter_phases(
        path,
        system_elements=elems,
        selected=selected,
        exclude_element_solids=body.exclude_element_solids,
        exclude_gases=body.exclude_gases,
    )
    return {
        "phases": [
            {"name": p.name, "elements": sorted(p.elements), "reaction": p.reaction}
            for p in phases
        ],
        "count": len(phases),
    }
