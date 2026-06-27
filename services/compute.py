"""Background compute job orchestration."""
from __future__ import annotations

import threading
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from ..api.dependencies import dll_path, resolve_db_record
from ..api.models import ComputeRequest
from ..diagram.packer import pack_grid_results
from ..diagram.phases import resolve_phase_names, system_elements_from_totals
from ..phreeqc.engine import GridJobParams, validate_phreeqc_setup
from ..phreeqc.sweep import run_grid_sweep

_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()


def create_job() -> str:
    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "status": "running",
            "progress": 0.0,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    return job_id


def get_job(job_id: str) -> dict[str, Any] | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
    return dict(job) if job else None


def get_job_result(job_id: str) -> dict[str, Any] | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job or job.get("status") != "done":
        return None
    return job.get("result")


def run_compute_job(job_id: str, body: ComputeRequest) -> None:
    thread = threading.Thread(target=_run_job, args=(job_id, body), daemon=True)
    thread.start()


def _run_job(job_id: str, body: ComputeRequest) -> None:
    try:
        db_rec = resolve_db_record(db_id=body.db_id, db_path=body.db_path)
        db = db_rec.path
        dll = dll_path(body.dll_path)
        system_elems = set(system_elements_from_totals(body.totals, body.system_elements))
        phase_names = resolve_phase_names(
            db,
            phases=body.phases,
            system_elems=system_elems,
        )

        validate_phreeqc_setup(dll, db)

        params = GridJobParams(
            db_path=db,
            dll_path=dll,
            temp_c=body.temp_c,
            ph_min=body.ph_min,
            ph_max=body.ph_max,
            ph_levels=body.ph_levels,
            pe_min=body.pe_min,
            pe_max=body.pe_max,
            pe_levels=body.pe_levels,
            totals=body.totals,
            phases=phase_names,
            system_elements=system_elements_from_totals(body.totals, body.system_elements),
            charge_species=body.charge_species,
            units=body.units,
        )

        def progress(done: int, total: int):
            with _jobs_lock:
                if job_id in _jobs:
                    _jobs[job_id]["progress"] = done / total

        results = run_grid_sweep(params, max_workers=body.max_workers, progress_cb=progress)
        rows = [asdict(r) for r in results]
        packed = pack_grid_results(params, rows, db_path=db)

        with _jobs_lock:
            _jobs[job_id].update(
                {
                    "status": "done",
                    "progress": 1.0,
                    "result": packed,
                    "raw_count": len(rows),
                    "phases_used": list(phase_names),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                }
            )
    except Exception as exc:
        with _jobs_lock:
            _jobs[job_id].update(
                {
                    "status": "error",
                    "error": str(exc),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                }
            )
