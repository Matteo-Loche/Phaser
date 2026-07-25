"""Per-job cancel tokens and ProcessPool hard-kill helpers.

Compute jobs register a ``threading.Event`` when they start running and bind
any live ``ProcessPoolExecutor``. Abort sets the event (cooperative checks)
and terminates child worker processes so a hung PHREEQC call cannot pin a
concurrent job slot forever.
"""
from __future__ import annotations

import multiprocessing as mp
import os
import signal
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures import TimeoutError as FuturesTimeout
from contextlib import contextmanager
from typing import Any, Callable, Iterator, TypeVar

T = TypeVar("T")
R = TypeVar("R")


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

# How often abortable pool waits wake to re-check the cancel event.
_POOL_WAIT_TIMEOUT_SEC = 0.5


def register_cancel_event(job_id: str) -> threading.Event:
    """Create/replace the cancel event for a job that is about to run."""
    ev = threading.Event()
    with _lock:
        _cancel_events[job_id] = ev
        _abort_meta.pop(job_id, None)
    return ev


def clear_job_control(job_id: str) -> None:
    """Drop cancel/pool registry entries for ``job_id``, terminating any leftover pool."""
    with _lock:
        _cancel_events.pop(job_id, None)
        _abort_meta.pop(job_id, None)
        pool = _pools.pop(job_id, None)
    if pool is not None:
        terminate_pool(pool)


def bind_pool(job_id: str, pool: ProcessPoolExecutor) -> None:
    with _lock:
        _pools[job_id] = pool


def unbind_pool(job_id: str, pool: ProcessPoolExecutor | None = None) -> None:
    with _lock:
        current = _pools.get(job_id)
        if pool is None or current is pool:
            _pools.pop(job_id, None)


def job_control_snapshot_for_tests() -> dict[str, Any]:
    """Registry contents for assertions (unit tests only)."""
    with _lock:
        return {
            "cancel_events": sorted(_cancel_events),
            "pools": sorted(_pools),
            "abort_meta": {k: v for k, v in _abort_meta.items()},
        }


def reset_job_control_for_tests() -> None:
    """Terminate any bound pools and clear abort registry (unit tests only)."""
    with _lock:
        pools = list(_pools.values())
        _cancel_events.clear()
        _abort_meta.clear()
        _pools.clear()
    for pool in pools:
        terminate_pool(pool)


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
    """Force-stop a process pool (SIGTERM/SIGKILL children, then non-blocking shutdown).

    Workers are killed *before* ``shutdown`` so a blocked PHREEQC call cannot
    keep the executor's queue-management thread draining forever.
    """
    pids = _collect_worker_pids(pool)
    procs = list((getattr(pool, "_processes", None) or {}).values())

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

    if pids:
        _signal_pids(pids, signal.SIGTERM)
        time.sleep(0.15)
        _signal_pids(pids, getattr(signal, "SIGKILL", signal.SIGTERM))

    try:
        pool.shutdown(wait=False, cancel_futures=True)
    except TypeError:
        try:
            pool.shutdown(wait=False)
        except Exception:
            pass
    except Exception:
        pass


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


def pool_map_abortable(
    pool: ProcessPoolExecutor,
    fn: Callable[[T], R],
    items: list[T],
    *,
    job_id: str | None,
    max_in_flight: int,
    on_result: Callable[[R, int], None] | None = None,
) -> list[R]:
    """Like ``pool.map`` but wakes every ``_POOL_WAIT_TIMEOUT_SEC`` to honour abort.

    ``pool.map`` blocks inside the result queue with no timeout, so after a wall
    timeout kills workers the job thread can hang forever and leave multiprocessing
    atexit hooks stuck on Ctrl+C minutes later. This helper uses ``wait(..., timeout=)``
    plus ``check_abort`` so the cancel event unblocks promptly.
    """
    if not items:
        return []
    in_flight = max(1, int(max_in_flight))
    pending: set = set()
    it = iter(items)
    out: list[R] = []
    done_n = 0

    def _submit_more() -> None:
        while len(pending) < in_flight:
            try:
                item = next(it)
            except StopIteration:
                return
            pending.add(pool.submit(fn, item))

    check_abort(job_id)
    _submit_more()
    while pending:
        check_abort(job_id)
        finished, not_done = wait(
            pending,
            timeout=_POOL_WAIT_TIMEOUT_SEC,
            return_when=FIRST_COMPLETED,
        )
        pending = set(not_done)
        if not finished:
            continue
        for fut in finished:
            check_abort(job_id)
            try:
                row = fut.result(timeout=0.1)
            except FuturesTimeout as exc:
                # Should be ready; treat as pool failure / abort race.
                check_abort(job_id)
                raise RuntimeError("Process pool future ready but result timed out") from exc
            done_n += 1
            out.append(row)
            if on_result is not None:
                on_result(row, done_n)
        _submit_more()
    return out


def iter_futures_abortable(
    futures_map: dict[Any, Any],
    *,
    job_id: str | None,
) -> Iterator[tuple[Any, Any]]:
    """Yield ``(future, key)`` as futures complete, abortable with a wait timeout."""
    pending = set(futures_map.keys())
    while pending:
        check_abort(job_id)
        finished, not_done = wait(
            pending,
            timeout=_POOL_WAIT_TIMEOUT_SEC,
            return_when=FIRST_COMPLETED,
        )
        pending = set(not_done)
        if not finished:
            continue
        for fut in finished:
            check_abort(job_id)
            yield fut, futures_map[fut]


def _shutdown_pool(pool: ProcessPoolExecutor, *, wait: bool) -> None:
    try:
        pool.shutdown(wait=wait, cancel_futures=not wait)
    except TypeError:
        try:
            pool.shutdown(wait=wait)
        except Exception:
            pass
    except Exception:
        pass


@contextmanager
def managed_process_pool(
    job_id: str | None,
    **executor_kwargs: Any,
) -> Iterator[ProcessPoolExecutor]:
    """``ProcessPoolExecutor`` that binds/unbinds for hard-kill by ``job_id``.

    The pool stays bound until shutdown finishes so a wall-timeout abort can still
    ``terminate_pool`` while we are inside ``shutdown(wait=True)``. Unbinding first
    left the job thread stuck forever with ``_running_count`` held, so queued jobs
    never started after a timeout.
    """
    kwargs = dict(executor_kwargs)
    # Prefer spawn so workers do not inherit the server listen socket (a killed
    # parent can otherwise leave an orphan worker holding :8765 on Linux fork).
    if "mp_context" not in kwargs:
        kwargs["mp_context"] = mp.get_context("spawn")
    pool = ProcessPoolExecutor(**kwargs)
    if job_id:
        bind_pool(job_id, pool)
    try:
        yield pool
    finally:
        try:
            if job_id and is_abort_requested(job_id):
                terminate_pool(pool)
                _shutdown_pool(pool, wait=False)
            else:
                # Keep pool bound: abort timer may kill workers mid-join.
                _shutdown_pool(pool, wait=True)
                if job_id and is_abort_requested(job_id):
                    terminate_pool(pool)
                    _shutdown_pool(pool, wait=False)
        finally:
            if job_id:
                unbind_pool(job_id, pool)
