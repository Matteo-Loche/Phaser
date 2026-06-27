"""Background compute job orchestration with a CPU-aware queue."""
from __future__ import annotations

import threading
import uuid
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from .. import config
from ..api.dependencies import dll_path, resolve_db_record
from ..api.models import ComputeRequest
from ..diagram.packer import pack_grid_results
from ..diagram.phases import resolve_phase_names, system_elements_from_totals
from ..phreeqc.engine import GridJobParams, validate_phreeqc_setup
from ..phreeqc.sweep import run_grid_sweep

_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()
_pending: deque[tuple[str, ComputeRequest]] = deque()
_running_count = 0


def _refresh_queue_positions_locked() -> None:
    size = len(_pending)
    for i, (job_id, _) in enumerate(_pending):
        if job_id in _jobs:
            _jobs[job_id]["queue_position"] = i + 1
            _jobs[job_id]["queue_size"] = size


def _try_dispatch() -> None:
    global _running_count
    to_start: list[tuple[str, ComputeRequest]] = []
    with _jobs_lock:
        while _running_count < config.MAX_CONCURRENT_JOBS and _pending:
            job_id, body = _pending.popleft()
            if job_id not in _jobs:
                continue
            _running_count += 1
            _jobs[job_id]["status"] = "running"
            _jobs[job_id]["queue_position"] = None
            _refresh_queue_positions_locked()
            to_start.append((job_id, body))

    for job_id, body in to_start:
        threading.Thread(
            target=_run_job_wrapper,
            args=(job_id, body),
            daemon=True,
        ).start()


def create_job() -> str:
    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "status": "queued",
            "progress": 0.0,
            "queue_position": len(_pending) + 1,
            "queue_size": len(_pending) + 1,
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


def delete_job(job_id: str) -> bool:
    global _pending
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return False

        if job.get("status") == "queued":
            _pending = deque((jid, body) for jid, body in _pending if jid != job_id)
            _refresh_queue_positions_locked()
            return _jobs.pop(job_id, None) is not None

        if job.get("status") == "running":
            # Let the worker finish; result will be discarded on delete after completion.
            return _jobs.pop(job_id, None) is not None

        return _jobs.pop(job_id, None) is not None


def run_compute_job(job_id: str, body: ComputeRequest) -> None:
    with _jobs_lock:
        if job_id not in _jobs:
            return
        _pending.append((job_id, body))
        _jobs[job_id]["queue_position"] = len(_pending)
        _jobs[job_id]["queue_size"] = len(_pending)
        _refresh_queue_positions_locked()
    _try_dispatch()


def _run_job_wrapper(job_id: str, body: ComputeRequest) -> None:
    global _running_count
    try:
        if get_job(job_id) is not None:
            _run_job(job_id, body)
    finally:
        with _jobs_lock:
            _running_count = max(0, _running_count - 1)
        _try_dispatch()


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
            if job_id not in _jobs:
                return
            _jobs[job_id].update(
                {
                    "status": "done",
                    "progress": 1.0,
                    "queue_position": None,
                    "result": packed,
                    "raw_count": len(rows),
                    "phases_used": list(phase_names),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                }
            )
    except Exception as exc:
        with _jobs_lock:
            if job_id not in _jobs:
                return
            _jobs[job_id].update(
                {
                    "status": "error",
                    "error": str(exc),
                    "queue_position": None,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                }
            )
