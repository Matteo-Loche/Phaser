"""Application configuration endpoint."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from ... import config
from ...chemistry.units import unit_label
from ...db.catalog_store import catalog_public_meta, list_elements, require_ready
from ...db.registry import get_default_database, list_databases
from ...services.species import species_suggestions

router = APIRouter(tags=["config"])


@router.get("/api/config")
def get_config():
    try:
        default_db = get_default_database()
    except RuntimeError:
        default_db = None

    db_elements: list[str] = []
    catalog_meta: dict = {"catalog_status": "missing", "catalog_error": None}
    if default_db and default_db.exists:
        catalog_meta = catalog_public_meta(default_db)
        try:
            db_key = require_ready(default_db)
            db_elements = list_elements(db_key)
        except RuntimeError:
            db_elements = []

    databases = []
    for rec in list_databases():
        payload = rec.public_dict()
        payload.update(catalog_public_meta(rec))
        databases.append(payload)

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
            "o2_limit_atm": config.O2_FUGACITY_LIMIT_ATM,
            "h2_limit_atm": config.H2_FUGACITY_LIMIT_ATM,
        },
        "known_totals": list(config.KNOWN_TOTALS),
        "unit_options": list(config.UNIT_OPTIONS),
        "unit_labels": {u: unit_label(u) for u in config.UNIT_OPTIONS},
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
        "job_result_ttl_sec": config.JOB_RESULT_TTL_SEC,
        "job_queue_ttl_sec": config.JOB_QUEUE_TTL_SEC,
        "db_exists": bool(default_db and default_db.exists),
        "dll_exists": Path(config.IPHREEQC_DLL).is_file(),
        **catalog_meta,
    }
