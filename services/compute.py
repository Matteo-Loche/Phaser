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
from .job_control import (
    JobAborted,
    check_abort,
    clear_job_control,
    register_cancel_event,
    request_abort,
)

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


def _wall_timeout_message(limit_sec: int | None = None) -> str:
    limit = int(limit_sec if limit_sec is not None else config.JOB_WALL_TIMEOUT_SEC)
    return f"Job exceeded wall-clock limit ({limit}s)."


def _mark_job_terminal_error(
    job_id: str,
    *,
    error: str,
    error_code: str,
    wall_timeout_sec: int | None = None,
) -> bool:
    """Mark a running job as terminal error. Caller may hold ``_jobs_lock`` or not."""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return False
        if job.get("status") not in ("running", "queued"):
            # Already terminal — keep first error_code (e.g. timed_out).
            return False
        payload: dict[str, Any] = {
            "status": "error",
            "error": error,
            "error_code": error_code,
            "queue_position": None,
            "finished_at": _utcnow().isoformat(),
        }
        if wall_timeout_sec is not None:
            payload["wall_timeout_sec"] = int(wall_timeout_sec)
        job.update(payload)
        return True


def abort_running_job(
    job_id: str,
    *,
    reason: str,
    error_code: str,
    wall_timeout_sec: int | None = None,
) -> bool:
    """Hard-abort a job: signal cancel, kill ProcessPool children, mark error."""
    request_abort(job_id, reason=reason, error_code=error_code)
    return _mark_job_terminal_error(
        job_id,
        error=reason,
        error_code=error_code,
        wall_timeout_sec=wall_timeout_sec,
    )


def running_jobs_past_deadline(
    now: datetime | None = None,
    *,
    limit_sec: int | None = None,
) -> list[str]:
    """Return job_ids of ``running`` jobs whose ``started_at`` exceeds the wall limit."""
    now = now or _utcnow()
    limit = int(limit_sec if limit_sec is not None else config.JOB_WALL_TIMEOUT_SEC)
    overdue: list[str] = []
    with _jobs_lock:
        for job_id, job in _jobs.items():
            if job.get("status") != "running":
                continue
            started = _parse_dt(job.get("started_at"))
            age = _age_seconds(started, now)
            if age is not None and age > limit:
                overdue.append(job_id)
    return overdue


def enforce_wall_timeouts(now: datetime | None = None) -> list[str]:
    """Abort every running job past the wall-clock deadline. Returns aborted ids."""
    limit = int(config.JOB_WALL_TIMEOUT_SEC)
    reason = _wall_timeout_message(limit)
    aborted: list[str] = []
    for job_id in running_jobs_past_deadline(now, limit_sec=limit):
        if abort_running_job(
            job_id,
            reason=reason,
            error_code="timed_out",
            wall_timeout_sec=limit,
        ):
            aborted.append(job_id)
        else:
            # Job may already be marked; still ensure pool kill.
            request_abort(job_id, reason=reason, error_code="timed_out")
            aborted.append(job_id)
    return aborted


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
        enforce_wall_timeouts()
        with _jobs_lock:
            _prune_jobs_locked()


def start_job_reaper() -> None:
    global _reaper_started
    if _reaper_started:
        return
    _reaper_started = True
    _reaper_stop.clear()
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
            register_cancel_event(job_id)
            _refresh_queue_positions_locked()
            to_start.append((job_id, body))

    for job_id, body in to_start:
        threading.Thread(
            target=_run_job_wrapper,
            args=(job_id, body),
            daemon=True,
            name=f"phaser-job-{job_id[:8]}",
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
        if not job:
            return None
        # Public view: omit non-JSON internals if any were attached.
        return {
            k: v
            for k, v in job.items()
            if not str(k).startswith("_")
        }


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
        status = job.get("status")

        if status == "queued":
            _pending = deque((jid, body) for jid, body in _pending if jid != job_id)
            _refresh_queue_positions_locked()
            clear_job_control(job_id)
            return _jobs.pop(job_id, None) is not None

        if status == "running":
            # Hard-abort outside the lock so pool terminate cannot deadlock.
            pass
        else:
            clear_job_control(job_id)
            return _jobs.pop(job_id, None) is not None

    # running: kill workers, mark cancelled, drop record (slot frees in wrapper finally)
    abort_running_job(
        job_id,
        reason="Job cancelled by client.",
        error_code="cancelled",
    )
    with _jobs_lock:
        clear_job_control(job_id)
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
    limit = max(1, int(config.JOB_WALL_TIMEOUT_SEC))
    timer = threading.Timer(
        limit,
        lambda: abort_running_job(
            job_id,
            reason=_wall_timeout_message(limit),
            error_code="timed_out",
            wall_timeout_sec=limit,
        ),
    )
    timer.daemon = True
    timer.start()
    try:
        with _jobs_lock:
            alive = job_id in _jobs
        if alive:
            _run_job(job_id, body, started_at_perf=t0)
    finally:
        timer.cancel()
        clear_job_control(job_id)
        with _jobs_lock:
            _running_count = max(0, _running_count - 1)
        _try_dispatch()


def _run_job(job_id: str, body: ComputeRequest, *, started_at_perf: float) -> None:
    db_rec = None
    adapt_stats: dict[str, Any] = {}
    n_phreeqc_runs: int | None = None
    try:
        check_abort(job_id)
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
        from ..phreeqc.dummy_medium import EXCLUDED_ELEMENTS

        sys_tuple = tuple(
            e
            for e in system_elements_from_totals(body.totals, body.system_elements)
            if e not in EXCLUDED_ELEMENTS
        )
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
            solution_mode=body.solution_mode,
        )

        def progress(done: int, total: int, phase: str = "compute"):
            check_abort(job_id)
            with _jobs_lock:
                if job_id in _jobs and _jobs[job_id].get("status") == "running":
                    _jobs[job_id]["progress"] = done / total if total else 0.0
                    _jobs[job_id]["phase"] = phase

        if body.adaptive_boundaries:
            pack_params, adapt_stats, base_results, trace_bundle = (
                run_adaptive_boundary_sweep(
                    params,
                    max_workers=config.MAX_WORKERS,
                    progress_cb=progress,
                    refine_factor=body.adaptive_refine_factor,
                    job_id=job_id,
                )
            )
            compute_mode = "adaptive"
        else:
            results, mask_stats = run_grid_sweep(
                params,
                max_workers=config.MAX_WORKERS,
                progress_cb=progress,
                job_id=job_id,
            )
            pack_params = params
            adapt_stats = dict(mask_stats)
            base_results = results
            trace_bundle = None
            compute_mode = "uniform"

        check_abort(job_id)
        rows = [asdict(r) for r in base_results]
        pack_layers = count_layer_pack_steps(pack_params)
        # Adaptive jobs pack the base hover grids and then the traced vector
        # display, each with the same layer count. Budget both passes so the
        # reported packing fraction is monotonic and never exceeds 100%.
        pack_total = pack_layers * (2 if trace_bundle else 1)

        def pack_tick(_step: int, _total: int) -> None:
            nonlocal pack_done
            check_abort(job_id)
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
            if job.get("status") != "running":
                return
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
    except JobAborted as exc:
        limit = int(config.JOB_WALL_TIMEOUT_SEC) if exc.error_code == "timed_out" else None
        _mark_job_terminal_error(
            job_id,
            error=str(exc.reason or exc),
            error_code=exc.error_code,
            wall_timeout_sec=limit,
        )
    except Exception as exc:
        # Pool death after hard abort often surfaces as BrokenProcessPool/OSError.
        try:
            check_abort(job_id)
        except JobAborted as aborted:
            limit = (
                int(config.JOB_WALL_TIMEOUT_SEC)
                if aborted.error_code == "timed_out"
                else None
            )
            _mark_job_terminal_error(
                job_id,
                error=str(aborted.reason or aborted),
                error_code=aborted.error_code,
                wall_timeout_sec=limit,
            )
            return
        with _jobs_lock:
            if job_id not in _jobs:
                return
            if _jobs[job_id].get("status") != "running":
                return
            _jobs[job_id].update(
                {
                    "status": "error",
                    "error": str(exc),
                    "queue_position": None,
                    "finished_at": _utcnow().isoformat(),
                }
            )


def _reset_runtime_state_for_tests() -> None:
    """Clear in-memory job queues (unit tests only)."""
    global _pending, _running_count
    with _jobs_lock:
        for job_id in list(_jobs.keys()):
            clear_job_control(job_id)
        _jobs.clear()
        _pending = deque()
        _running_count = 0
