"""Background compute job orchestration with a CPU-aware queue."""
from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from .. import config
from ..api.dependencies import resolve_db_record
from ..api.models import ComputeRequest
from ..chemistry.units import is_valid_unit, normalize_unit, totals_to_mmol_kgw
from ..diagram.packer import count_layer_pack_steps, effective_layer_elements, pack_grid_results
from ..diagram.phases import resolve_phase_names, system_elements_from_totals
from ..diagram.vectors import pack_traced_display
from ..phreeqc.engine import GridJobParams, validate_phreeqc_setup
from ..phreeqc.adaptive import run_adaptive_boundary_sweep
from ..phreeqc.sweep import run_grid_sweep

_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()
_pending: deque[tuple[str, ComputeRequest]] = deque()
_running_count = 0
_reaper_stop = threading.Event()
_reaper_started = False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_seconds(since: datetime | None, now: datetime) -> float | None:
    if since is None:
        return None
    return (now - since).total_seconds()


def _prune_jobs_locked(now: datetime | None = None) -> int:
    """Remove stale queued and finished jobs. Caller must hold _jobs_lock."""
    global _pending
    now = now or _utcnow()
    removed = 0

    for job_id in list(_jobs.keys()):
        job = _jobs[job_id]
        status = job.get("status")

        if status == "queued":
            created = _parse_dt(job.get("created_at"))
            age = _age_seconds(created, now)
            if age is not None and age > config.JOB_QUEUE_TTL_SEC:
                _jobs.pop(job_id, None)
                _pending = deque((jid, body) for jid, body in _pending if jid != job_id)
                removed += 1
            continue

        if status in ("done", "error"):
            finished = _parse_dt(job.get("finished_at")) or _parse_dt(job.get("created_at"))
            age = _age_seconds(finished, now)
            if age is not None and age > config.JOB_RESULT_TTL_SEC:
                _jobs.pop(job_id, None)
                removed += 1

    if removed:
        _refresh_queue_positions_locked()
    return removed


def _reaper_loop() -> None:
    while not _reaper_stop.wait(config.JOB_REAPER_INTERVAL_SEC):
        with _jobs_lock:
            _prune_jobs_locked()


def start_job_reaper() -> None:
    global _reaper_started
    if _reaper_started:
        return
    _reaper_started = True
    threading.Thread(target=_reaper_loop, daemon=True, name="phaser-job-reaper").start()


def stop_job_reaper() -> None:
    _reaper_stop.set()


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
            started = _utcnow()
            _jobs[job_id]["started_at"] = started.isoformat()
            created = _parse_dt(_jobs[job_id].get("created_at"))
            # Jobs ahead (0 = started immediately) is captured at enqueue time;
            # only count real queue wait when something was actually ahead, so a
            # job that runs right away records exactly 0 instead of dispatch jitter.
            jobs_ahead = _jobs[job_id].get("queue_position_at_start") or 0
            if created is not None and jobs_ahead > 0:
                _jobs[job_id]["queue_wait_ms"] = (started - created).total_seconds() * 1000.0
            else:
                _jobs[job_id]["queue_wait_ms"] = 0.0
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
    now = _utcnow().isoformat()
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "status": "queued",
            "progress": 0.0,
            "queue_position": len(_pending) + 1,
            "queue_size": len(_pending) + 1,
            "created_at": now,
            "last_seen_at": now,
        }
        _prune_jobs_locked()
    return job_id


def get_job(job_id: str) -> dict[str, Any] | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job:
            job["last_seen_at"] = _utcnow().isoformat()
        _prune_jobs_locked()
    return dict(job) if job else None


def get_job_result(job_id: str) -> dict[str, Any] | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job:
            job["last_seen_at"] = _utcnow().isoformat()
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
        # Number of jobs that must finish before this one starts: those already
        # running plus those queued ahead of it. 0 means it starts immediately.
        _jobs[job_id]["queue_position_at_start"] = _running_count + (len(_pending) - 1)
        _refresh_queue_positions_locked()
    _try_dispatch()


def _run_job_wrapper(job_id: str, body: ComputeRequest) -> None:
    global _running_count
    t0 = time.perf_counter()
    try:
        if get_job(job_id) is not None:
            _run_job(job_id, body, started_at_perf=t0)
    finally:
        with _jobs_lock:
            _running_count = max(0, _running_count - 1)
        _try_dispatch()


def _run_job(job_id: str, body: ComputeRequest, *, started_at_perf: float) -> None:
    db_rec = None
    adapt_stats: dict[str, Any] = {}
    n_phreeqc_runs: int | None = None
    try:
        db_rec = resolve_db_record(db_id=body.db_id, db_path=body.db_path)
        db = db_rec.path
        dll = config.IPHREEQC_DLL
        system_elems = set(system_elements_from_totals(body.totals, body.system_elements))
        phase_names = resolve_phase_names(
            db_rec,
            phases=body.phases,
            system_elems=system_elems,
        )

        validate_phreeqc_setup(dll, db)

        from ..db.catalog_store import (
            list_collisions,
            list_gas_phases,
            list_trace_gas_phases,
            phase_names_by_subset_map,
            require_ready,
        )

        db_key = require_ready(db_rec)
        sys_tuple = system_elements_from_totals(body.totals, body.system_elements)
        layer_elements = effective_layer_elements(sys_tuple, body.layer_elements)
        if body.gas_phases:
            trace_gases = tuple(body.gas_phases)
        elif body.include_common_gases:
            trace_gases = list_trace_gas_phases(db_key, sys_tuple)
        else:
            trace_gases = ()
        input_units = normalize_unit(body.units)
        if not is_valid_unit(input_units):
            raise ValueError(f"Unsupported concentration unit: {body.units!r}")
        totals_mmol = totals_to_mmol_kgw(body.totals, input_units)
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
            totals=totals_mmol,
            phases=phase_names,
            system_elements=sys_tuple,
            units=config.DEFAULT_UNITS,
            solid_aqueous_collisions=tuple(sorted(list_collisions(db_key))),
            phase_names_by_subset=phase_names_by_subset_map(db_key, sys_tuple),
            gas_phases=list_gas_phases(db_key),
            trace_gas_phases=trace_gases,
            o2_limit_atm=body.o2_limit_atm,
            h2_limit_atm=body.h2_limit_atm,
            layer_solids=body.layer_solids,
            layer_aqueous=body.layer_aqueous,
            layer_elements=layer_elements,
        )

        def progress(done: int, total: int, phase: str = "compute"):
            with _jobs_lock:
                if job_id in _jobs:
                    _jobs[job_id]["progress"] = done / total if total else 0.0
                    _jobs[job_id]["phase"] = phase

        if body.adaptive_boundaries:
            pack_params, adapt_stats, base_results, trace_bundle = (
                run_adaptive_boundary_sweep(
                    params,
                    max_workers=config.MAX_WORKERS,
                    progress_cb=progress,
                    refine_factor=body.adaptive_refine_factor,
                )
            )
            compute_mode = "adaptive"
        else:
            results = run_grid_sweep(
                params, max_workers=config.MAX_WORKERS, progress_cb=progress
            )
            pack_params = params
            adapt_stats = {}
            base_results = results
            trace_bundle = None
            compute_mode = "uniform"

        rows = [asdict(r) for r in base_results]
        pack_layers = count_layer_pack_steps(pack_params)
        # Adaptive jobs pack the base hover grids and then the traced vector
        # display, each with the same layer count. Budget both passes so the
        # reported packing fraction is monotonic and never exceeds 100%.
        pack_total = pack_layers * (2 if trace_bundle else 1)

        def pack_tick(_step: int, _total: int) -> None:
            nonlocal pack_done
            pack_done += 1
            progress(min(pack_done, pack_total), pack_total, "packing")

        pack_done = 0
        progress(0, pack_total, "packing")
        packed = pack_grid_results(
            pack_params, rows, db_path=db, progress_cb=pack_tick
        )
        packed["compute_mode"] = compute_mode
        if adapt_stats:
            packed["adaptive_stats"] = adapt_stats
        if trace_bundle:
            packed["display"] = pack_traced_display(
                pack_params,
                rows,
                trace_bundle,
                db_path=db,
                progress_cb=pack_tick,
            )
        progress(pack_total, pack_total, "packing")

        with _jobs_lock:
            if job_id not in _jobs:
                return
            job = _jobs[job_id]
            job.update(
                {
                    "status": "done",
                    "progress": 1.0,
                    "queue_position": None,
                    "result": packed,
                    "raw_count": len(rows),
                    "phases_used": list(phase_names),
                    "finished_at": _utcnow().isoformat(),
                }
            )
            queue_position_at_start = job.get("queue_position_at_start")
            queue_wait_ms = job.get("queue_wait_ms")
        compute_ms = (time.perf_counter() - started_at_perf) * 1000.0
        if adapt_stats:
            n_phreeqc_runs = adapt_stats.get("n_evaluated")
        if n_phreeqc_runs is None:
            n_phreeqc_runs = len(rows)
        if db_rec is not None:
            from .stats import record_compute

            record_compute(
                body,
                db_rec=db_rec,
                compute_ms=compute_ms,
                n_phreeqc_runs=n_phreeqc_runs,
                queue_position_at_start=queue_position_at_start,
                queue_wait_ms=queue_wait_ms,
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
                    "finished_at": _utcnow().isoformat(),
                }
            )
