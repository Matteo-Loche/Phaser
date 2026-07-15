"""Per-job cancel tokens and ProcessPool hard-kill helpers.

Compute jobs register a ``threading.Event`` when they start running and bind
any live ``ProcessPoolExecutor``. Abort sets the event (cooperative checks)
and terminates child worker processes so a hung PHREEQC call cannot pin a
concurrent job slot forever.
"""
from __future__ import annotations

import os
import signal
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from typing import Any, Iterator


class JobAborted(Exception):
    """Raised when a running job is aborted (wall timeout or DELETE)."""

    def __init__(self, reason: str = "aborted", *, error_code: str = "cancelled") -> None:
        self.reason = reason
        self.error_code = error_code
        super().__init__(reason)


_lock = threading.Lock()
_cancel_events: dict[str, threading.Event] = {}
_abort_meta: dict[str, tuple[str, str]] = {}  # job_id -> (reason, error_code)
_pools: dict[str, ProcessPoolExecutor] = {}


def register_cancel_event(job_id: str) -> threading.Event:
    """Create/replace the cancel event for a job that is about to run."""
    ev = threading.Event()
    with _lock:
        _cancel_events[job_id] = ev
        _abort_meta.pop(job_id, None)
    return ev


def clear_job_control(job_id: str) -> None:
    with _lock:
        _cancel_events.pop(job_id, None)
        _abort_meta.pop(job_id, None)
        _pools.pop(job_id, None)


def bind_pool(job_id: str, pool: ProcessPoolExecutor) -> None:
    with _lock:
        _pools[job_id] = pool


def unbind_pool(job_id: str, pool: ProcessPoolExecutor | None = None) -> None:
    with _lock:
        current = _pools.get(job_id)
        if pool is None or current is pool:
            _pools.pop(job_id, None)


def is_abort_requested(job_id: str | None) -> bool:
    if not job_id:
        return False
    with _lock:
        ev = _cancel_events.get(job_id)
    return bool(ev and ev.is_set())


def abort_meta(job_id: str) -> tuple[str, str] | None:
    with _lock:
        return _abort_meta.get(job_id)


def check_abort(job_id: str | None) -> None:
    """Raise ``JobAborted`` if this job has been cancelled or timed out."""
    if not job_id:
        return
    with _lock:
        ev = _cancel_events.get(job_id)
        meta = _abort_meta.get(job_id)
    if ev and ev.is_set():
        reason, code = meta or ("Job aborted", "cancelled")
        raise JobAborted(reason, error_code=code)


def _collect_worker_pids(pool: ProcessPoolExecutor) -> list[int]:
    procs = list((getattr(pool, "_processes", None) or {}).values())
    pids: list[int] = []
    for proc in procs:
        try:
            pid = proc.pid
            if pid is not None:
                pids.append(int(pid))
        except Exception:
            continue
    return pids


def _signal_pids(pids: list[int], sig: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def terminate_pool(pool: ProcessPoolExecutor) -> None:
    """Force-stop a process pool (cancel futures + SIGTERM/SIGKILL children)."""
    # Snapshot PIDs before shutdown clears internal maps.
    pids = _collect_worker_pids(pool)
    procs = list((getattr(pool, "_processes", None) or {}).values())

    try:
        pool.shutdown(wait=False, cancel_futures=True)
    except TypeError:
        pool.shutdown(wait=False)
    except Exception:
        pass

    for proc in procs:
        try:
            if proc.is_alive():
                proc.terminate()
        except Exception:
            pass
    for proc in procs:
        try:
            proc.join(timeout=0.4)
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=0.4)
        except Exception:
            pass

    # Last resort: signal by PID (covers executor Process wrappers that mis-report).
    if pids:
        _signal_pids(pids, signal.SIGTERM)
        time.sleep(0.15)
        # SIGKILL is POSIX-only; on Windows fall back to SIGTERM again.
        _signal_pids(pids, getattr(signal, "SIGKILL", signal.SIGTERM))


def request_abort(
    job_id: str,
    *,
    reason: str,
    error_code: str = "cancelled",
) -> bool:
    """Signal abort and hard-kill any registered pool for ``job_id``.

    Returns True if a cancel event existed (job was registered as running).
    """
    with _lock:
        ev = _cancel_events.get(job_id)
        pool = _pools.get(job_id)
        if ev is not None:
            _abort_meta[job_id] = (reason, error_code)
            ev.set()
        registered = ev is not None
    if pool is not None:
        terminate_pool(pool)
    return registered


@contextmanager
def managed_process_pool(
    job_id: str | None,
    **executor_kwargs: Any,
) -> Iterator[ProcessPoolExecutor]:
    """``ProcessPoolExecutor`` that binds/unbinds for hard-kill by ``job_id``.

    Shutdown is managed manually so an aborted pool is not blocked on
    ``shutdown(wait=True)`` waiting for hanged workers.
    """
    pool = ProcessPoolExecutor(**executor_kwargs)
    if job_id:
        bind_pool(job_id, pool)
    aborted = False
    try:
        yield pool
    finally:
        if job_id:
            aborted = is_abort_requested(job_id)
            unbind_pool(job_id, pool)
        if aborted:
            terminate_pool(pool)
            try:
                pool.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                pool.shutdown(wait=False)
            except Exception:
                pass
        else:
            try:
                pool.shutdown(wait=True, cancel_futures=False)
            except TypeError:
                pool.shutdown(wait=True)
            except Exception:
                pass
