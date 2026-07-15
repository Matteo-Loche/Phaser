"""Application configuration endpoint."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from ... import config
from ...__version__ import __version__
from ...chemistry.units import unit_label
from ...db.catalog_store import catalog_public_meta, list_elements, require_ready
from ...db.registry import get_default_database, list_enabled_databases
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
    for rec in list_enabled_databases():
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
        "max_workers": config.MAX_WORKERS,
        "max_concurrent_jobs": config.MAX_CONCURRENT_JOBS,
        "default_solution_mode": config.SOLUTION_MODE_DEFAULT,
        "solution_modes": [
            {"id": mode_id, **config.SOLUTION_MODE_META[mode_id]}
            for mode_id in config.SOLUTION_MODES
        ],
        "adaptive_boundaries_default": config.ADAPTIVE_BOUNDARIES_DEFAULT,
        "adaptive_refine_factor": config.ADAPTIVE_REFINE_FACTOR,
        "max_adaptive_points": config.MAX_ADAPTIVE_POINTS,
        "job_result_ttl_sec": config.JOB_RESULT_TTL_SEC,
        "job_queue_ttl_sec": config.JOB_QUEUE_TTL_SEC,
        "job_wall_timeout_sec": config.JOB_WALL_TIMEOUT_SEC,
        "rate_limits": {
            "enabled": config.RATE_LIMIT_ENABLED,
            "window_sec": config.RATE_LIMIT_WINDOW_SEC,
            "api_per_min": config.RATE_LIMIT_API_PER_MIN,
            "compute_per_min": config.RATE_LIMIT_COMPUTE_PER_MIN,
            "db_register_per_min": config.RATE_LIMIT_DB_REGISTER_PER_MIN,
            "phases_per_min": config.RATE_LIMIT_PHASES_PER_MIN,
            "compute_cooldown_sec": config.RATE_LIMIT_COMPUTE_COOLDOWN_SEC,
            "db_register_cooldown_sec": config.RATE_LIMIT_DB_REGISTER_COOLDOWN_SEC,
            "cooldown_escalate": config.RATE_LIMIT_COOLDOWN_ESCALATE,
            "cooldown_max_sec": config.RATE_LIMIT_COOLDOWN_MAX_SEC,
            "violation_reset_sec": config.RATE_LIMIT_VIOLATION_RESET_SEC,
        },
        "db_exists": bool(default_db and default_db.exists),
        "dll_exists": Path(config.IPHREEQC_DLL).is_file(),
        "about": {
            "app_version": __version__,
            "build_id": config.BUILD_ID,
            "repository_url": config.REPOSITORY_URL,
            "issues_url": config.ISSUES_URL,
            "license_name": config.LICENSE_NAME,
            "license_url": config.LICENSE_URL,
            "doi_url": config.DOI_URL,
        },
        **catalog_meta,
    }
