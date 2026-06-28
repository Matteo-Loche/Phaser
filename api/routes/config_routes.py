"""Application configuration endpoint."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from ... import config
from ...db.parser import list_elements
from ...db.registry import get_default_database, list_databases
from ...services.species import species_suggestions

router = APIRouter(tags=["config"])


@router.get("/api/config")
def get_config():
    try:
        default_db = get_default_database()
    except RuntimeError:
        default_db = None

    db_path = default_db.path if default_db else config.THERMODDEM_DB
    db_elements = list_elements(db_path) if Path(db_path).is_file() else []
    databases = [rec.public_dict() for rec in list_databases()]
    return {
        "default_db_id": default_db.id if default_db else None,
        "databases": databases,
        "dll_path": config.IPHREEQC_DLL,
        "host": config.HOST,
        "port": config.PORT,
        "defaults": {
            "temp_c": config.TEMP_C,
            "ph_min": config.PH_MIN,
            "ph_max": config.PH_MAX,
            "pe_min": config.PE_MIN,
            "pe_max": config.PE_MAX,
            "grid_levels": config.GRID_LEVELS,
        },
        "known_totals": list(config.KNOWN_TOTALS),
        "unit_options": list(config.UNIT_OPTIONS),
        "default_units": config.DEFAULT_UNITS,
        "default_species_conc": config.DEFAULT_SPECIES_CONC,
        "db_elements": db_elements,
        "species_suggestions": species_suggestions(db_elements),
        "max_phases": config.MAX_PHASES_PER_JOB,
        "max_grid_points": config.MAX_GRID_POINTS,
        "max_concurrent_jobs": config.MAX_CONCURRENT_JOBS,
        "adaptive_boundaries_default": config.ADAPTIVE_BOUNDARIES_DEFAULT,
        "adaptive_refine_factor": config.ADAPTIVE_REFINE_FACTOR,
        "max_adaptive_points": config.MAX_ADAPTIVE_POINTS,
        "db_exists": bool(default_db and default_db.exists),
        "dll_exists": Path(config.IPHREEQC_DLL).is_file(),
    }
