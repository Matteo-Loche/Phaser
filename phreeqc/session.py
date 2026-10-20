"""One spawn pool for a job's PHREEQC phases (grid, trace, contours)."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from typing import Any, Iterator

from .. import config
from ..services.job_control import managed_process_pool
from .engine import GridJobParams, bind_worker_session


class ComputePool:
    """The live process pool for one job.

    Abort and the wall-clock timer kill whichever pool ``bind_pool`` holds.
    That registry stores a single pool, so every phase submits here instead of
    opening another executor. The totals slab is released by the contour pass,
    not by this object.
    """

    def __init__(self, pool: Any, job_id: str | None, workers: int) -> None:
        self.pool = pool
        self.job_id = job_id
        self.workers = workers


def require_compute_pool(compute_pool: ComputePool | None) -> ComputePool:
    """PHREEQC stages do not open a pool of their own."""
    if compute_pool is None:
        raise RuntimeError(
            "PHREEQC work requires the session pool from open_compute_pool. "
            "This call does not open a pool of its own."
        )
    return compute_pool


@contextmanager
def open_compute_pool(
    job_id: str | None,
    params: GridJobParams,
    *,
    max_workers: int | None = None,
) -> Iterator[ComputePool]:
    """Spawn workers once and keep them until PHREEQC work for this job is done.

    Packing stays outside this block: the rows are already on the parent, and
    the wall-clock limit does not cover packing.
    """
    workers = max(1, int(max_workers if max_workers is not None else config.MAX_WORKERS))
    with managed_process_pool(
        job_id,
        max_workers=workers,
        initializer=bind_worker_session,
        initargs=(params.dll_path, params.db_path, asdict(params)),
    ) as pool:
        yield ComputePool(pool, job_id, workers)
