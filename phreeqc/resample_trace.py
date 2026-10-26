"""Retrace mixed cells that are not a single coarse chord.

A two-category cell with two edge zeros keeps that chord. Every other mixed
cell is left undrawn at the coarse spacing and traced again, together with
the non-chord neighbours out to ``pad`` cells. The fine step is the coarse
step divided by ``sub`` (Δ_fine = Δ_coarse / sub). The cells form solid
rectangles, so a kept chord is never inside a fine window and the packer
cannot paint a neighbour across it.
Fine cells use the same chord, triple, band, saddle and fallback rules. This
pass does not open a pool: the session pool from the coarse trace is still open.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from ..services.job_control import check_abort, iter_futures_abortable
from .boundary_trace import (
    TraceStats,
    _assign_trace_batches,
    _chunk_cells,
    _merge_stats,
    trace_corner_payload,
)
from .engine import ResultView, grid_job_params_from_dict, point_key
from .session import require_compute_pool


def _fine_axis(values: np.ndarray, sub: int) -> np.ndarray:
    """Place ``sub`` equal spaces in each coarse step, so Δ_fine = Δ_coarse / sub."""
    if len(values) < 2:
        return np.asarray(values, dtype=float)
    out: list[float] = []
    for left, right in zip(values, values[1:]):
        for k in range(sub):
            if k == 0:
                out.append(float(left))
            else:
                out.append(float(left) + (float(right) - float(left)) * k / sub)
    out.append(float(values[-1]))
    return np.asarray(out, dtype=float)


def _resampled_cells(
    seeds: list[list[int]],
    chord_cells: set[tuple[int, int]],
    pad: int,
    n_ph: int,
    n_pe: int,
) -> set[tuple[int, int]]:
    """Non-chord cells, plus non-chord neighbours out to ``pad`` cells.

    A cell the coarse pass already drew as one chord stays out, so it is
    neither solved again nor covered by a fine fill. ``pad`` is the Chebyshev
    reach: 1 is the eight adjacent cells.
    """
    found: set[tuple[int, int]] = set()
    for cell in seeds:
        i, j = int(cell[0]), int(cell[1])
        for di in range(-pad, pad + 1):
            ni = i + di
            if ni < 0 or ni >= n_ph - 1:
                continue
            for dj in range(-pad, pad + 1):
                nj = j + dj
                if nj < 0 or nj >= n_pe - 1 or (ni, nj) in chord_cells:
                    continue
                found.add((ni, nj))
    return found


def _solid_windows(cells: set[tuple[int, int]]) -> list[tuple[int, int, int, int]]:
    """Rectangles filled entirely by ``cells``.

    Consecutive cells in one row form a strip. A strip grows into the next
    row only when that row covers the same pH range. A missing cell breaks
    the strip. A kept chord between two resampled cells therefore stays
    outside every window. The packer paints an untraced cell from its
    lower-left node, so a chord inside the rectangle would be covered by
    the neighbour's colour.

    Each rectangle is ``(i0, i1, j0, j1)`` with ``i1`` and ``j1`` exclusive,
    matching the node span ``base_ph[i0 : i1 + 1]``.
    """
    by_row: dict[int, list[int]] = {}
    for i, j in cells:
        by_row.setdefault(int(j), []).append(int(i))
    if not by_row:
        return []
    runs_by_row: dict[int, list[tuple[int, int]]] = {}
    for j, cols in by_row.items():
        ordered = sorted(cols)
        runs: list[tuple[int, int]] = []
        start = previous = ordered[0]
        for i in ordered[1:]:
            if i == previous + 1:
                previous = i
                continue
            runs.append((start, previous + 1))
            start = previous = i
        runs.append((start, previous + 1))
        runs_by_row[j] = runs

    active: list[list[int]] = []
    out: list[tuple[int, int, int, int]] = []
    previous_j: int | None = None
    for j in sorted(runs_by_row):
        if previous_j is not None and j != previous_j + 1:
            out.extend(tuple(rec) for rec in active)
            active = []
        used = [False] * len(active)
        fresh: list[list[int]] = []
        for i0, i1 in runs_by_row[j]:
            matched = False
            for k, rec in enumerate(active):
                if used[k] or rec[0] != i0 or rec[1] != i1 or rec[3] != j:
                    continue
                rec[3] = j + 1
                used[k] = True
                matched = True
                break
            if not matched:
                fresh.append([i0, i1, j, j + 1])
        out.extend(tuple(rec) for k, rec in enumerate(active) if not used[k])
        active = [rec for k, rec in enumerate(active) if used[k]]
        active.extend(fresh)
        previous_j = j
    out.extend(tuple(rec) for rec in active)
    return out


def _window_work(
    i0: int,
    i1: int,
    j0: int,
    j1: int,
    resampled: set[tuple[int, int]],
    sub: int,
    base_ph: np.ndarray,
    base_pe: np.ndarray,
    known: dict[tuple[float, float], Any],
) -> dict[str, Any]:
    """Fine nodes of the non-chord coarse cells inside one solid rectangle."""
    fine_ph = _fine_axis(np.asarray(base_ph[i0 : i1 + 1], dtype=float), sub)
    fine_pe = _fine_axis(np.asarray(base_pe[j0 : j1 + 1], dtype=float), sub)
    nodes: set[tuple[int, int]] = set()
    covered: list[tuple[int, int]] = []
    for i, j in resampled:
        if i < i0 or i + 1 > i1 or j < j0 or j + 1 > j1:
            continue
        covered.append((i, j))
        fi0 = (i - i0) * sub
        fj0 = (j - j0) * sub
        for fj in range(fj0, fj0 + sub + 1):
            for fi in range(fi0, fi0 + sub + 1):
                nodes.add((fi, fj))
    missing: list[tuple[float, float]] = []
    for fi, fj in nodes:
        ph = float(fine_ph[fi])
        pe = float(fine_pe[fj])
        if point_key(ph, pe) not in known:
            missing.append((ph, pe))
    return {
        "i0": i0,
        "j0": j0,
        "fine_ph": fine_ph,
        "fine_pe": fine_pe,
        "nodes": nodes,
        "covered": covered,
        "missing": missing,
    }


def _blank_layer() -> dict[str, list]:
    """Records ``_pack_one_layer`` reads for one window."""
    return {
        "cell_lines": [],
        "cell_regions": [],
        "node_fi": [],
        "node_fj": [],
        "node_cat": [],
        "boundaries": [],
    }


def _trace_window_job(job: dict[str, Any]) -> dict[str, Any]:
    """Trace one layer's fine cells on this window's axis.

    The worker is already armed on the coarse axis. Swapping the axis clears
    the crossing cache, which is keyed by cell index, and keeps the pH–pe
    cache. The database is not reloaded. Stability limits stay on the coarse
    pass: a second pass on the fine grid would draw the same frontier twice.
    """
    from . import boundary_trace as bt

    bt._ensure_trace_state(job["cfg"])
    bt._retarget_trace_axis(job["cfg"]["base_ph"], job["cfg"]["base_pe"])
    if bt._WORKER_EVAL is None or bt._WORKER_TRACE_PARAMS is None:
        raise RuntimeError("The trace worker has no point evaluator.")
    specs = [spec for spec in (bt._WORKER_SPECS or []) if spec.layer_id == job["layer_id"]]
    if not specs:
        known = [spec.layer_id for spec in (bt._WORKER_SPECS or [])]
        raise LookupError(
            f"Layer {job['layer_id']!r} is not produced by this trace. "
            f"Known layers: {known}."
        )
    corner_ij = bt._decode_corners(job["corner_ij"])
    parts = []
    for idx, cells in job["chunks"]:
        layers_out, stability, stats = bt._trace_cells_batch(
            cells,
            bt._WORKER_TRACE_PARAMS,
            bt._WORKER_TRACE_PARAMS.db_path,
            np.asarray(job["cfg"]["base_ph"], dtype=float),
            np.asarray(job["cfg"]["base_pe"], dtype=float),
            corner_ij,
            tol=bt._WORKER_TOL,
            stability_tol=bt._WORKER_STABILITY_TOL,
            factor=bt._WORKER_FACTOR,
            specs=specs,
            resolve_pair=bt._WORKER_RESOLVE,
            evaluator=bt._WORKER_EVAL,
            resample=False,
            trace_stability=False,
        )
        parts.append(
            {
                "idx": idx,
                "window": int(job["window"]),
                "layer_id": job["layer_id"],
                "layers": layers_out,
                "stability": stability,
                "stats": stats,
            }
        )
        # The parent polls this while the job is still running. One future
        # covers every chunk on this worker, so the future alone would move
        # the bar only when the whole job returns.
        counter = job.get("counter")
        if counter is not None:
            with job["counter_lock"]:
                counter.value += 1
    return {"parts": parts}


def _restore_coarse_axis(pool, workers: int, cfg: dict[str, Any], job_id: str | None) -> None:
    """Point every worker back at the coarse axis and reload its database.

    A later stage on this same pool reads ``_WORKER_BASE_PH`` and continues
    the solver from the previous solution. The reload drops the fine-grid
    solution so that stage does not follow it.
    """
    armed = {
        pool.submit(_restore_axis_job, cfg): i for i in range(max(int(workers), 1))
    }
    for future, _idx in iter_futures_abortable(armed, job_id=job_id):
        future.result(timeout=0.1)


def _restore_axis_job(cfg: dict[str, Any]) -> bool:
    """Return this worker to the coarse axis and a freshly loaded database.

    The fine sweep and the fine trace leave the solver on whichever solution
    that process ran last. The next stage on this pool, such as element-total
    contours, would follow that solution. Reloading the database is the same
    step the coarse trace uses when it takes over from the grid.
    """
    from . import boundary_trace as bt
    from .engine import reset_worker_phreeqc

    bt._ensure_trace_state(cfg)
    bt._retarget_trace_axis(cfg["base_ph"], cfg["base_pe"])
    reset_worker_phreeqc()
    return True


def _report(progress_cb, done: int, total: int, phase: str) -> None:
    """Forward ``(done, total, phase)`` when the caller accepts a phase name."""
    if progress_cb is None:
        return
    progress_cb(int(done), max(int(total), 1), phase)


def _collect_window_jobs(
    pool,
    fine_jobs: list[dict[str, Any]],
    stats: TraceStats,
    window_meta: list[dict[str, Any]],
    progress_cb,
    job_id: str | None,
) -> None:
    """Run every fine-trace job and report phase ``restrace`` once per chunk.

    The counter is a manager proxy, same as the coarse trace: a spawn worker
    cannot take a raw shared integer, and the parent has to see a chunk finish
    before the worker's future returns.
    """
    import multiprocessing as mp
    from concurrent.futures import FIRST_COMPLETED, wait

    manager = mp.get_context("spawn").Manager()
    try:
        counter = manager.Value("i", 0)
        counter_lock = manager.Lock()
        n_chunks = 0
        for job in fine_jobs:
            job["counter"] = counter
            job["counter_lock"] = counter_lock
            n_chunks += len(job["chunks"])
        _report(progress_cb, 0, n_chunks, "restrace")
        futures = [pool.submit(_trace_window_job, job) for job in fine_jobs]
        pending_futures = set(futures)
        last = 0
        while pending_futures:
            check_abort(job_id)
            current = int(counter.value)
            if current != last:
                _report(progress_cb, current, n_chunks, "restrace")
                last = current
            finished, not_done = wait(
                pending_futures, timeout=0.5, return_when=FIRST_COMPLETED
            )
            pending_futures = set(not_done)
            for fut in finished:
                check_abort(job_id)
                for part in fut.result(timeout=0.1)["parts"]:
                    _merge_stats(stats, part["stats"])
                    meta = window_meta[int(part["window"])]
                    data = part["layers"].get(meta["layer_id"]) or {}
                    dest = meta["layer"]
                    for key in dest:
                        dest[key].extend(data.get(key) or [])
        _report(progress_cb, n_chunks, n_chunks, "restrace")
    finally:
        manager.shutdown()


def fill_resample_windows(
    pool,
    compute_pool,
    layers_out: dict[str, Any],
    stats: TraceStats,
    *,
    params,
    base_ph: np.ndarray,
    base_pe: np.ndarray,
    base_ij: dict[tuple[int, int], Any],
    trace_cfg: dict[str, Any],
    job_id: str | None,
    pad: int,
    sub: int,
    progress_cb=None,
) -> None:
    """Sweep missing fine points once, then trace every solid window.

    Windows are stored on the layer for the packer. Their node indexes belong
    to the window axis, not to ``i_coarse * factor``, so they are not merged
    into the coarse node lists. ``skip_cells`` tells the coarse pack to leave
    those rectangles empty; the window pack fills them afterwards.
    """
    require_compute_pool(compute_pool)
    check_abort(job_id)
    if pad < 0:
        raise ValueError(f"PHASER_RESAMPLE_PAD must be 0 or greater, got {pad}.")
    if sub < 2:
        raise ValueError(
            f"Resampling needs at least 2 intervals per coarse cell, got {sub}."
        )
    n_ph = len(base_ph)
    n_pe = len(base_pe)
    cat_fns = _category_fns(trace_cfg)
    known: dict[tuple[float, float], Any] = {}
    for (i, j), row in base_ij.items():
        known[point_key(float(base_ph[i]), float(base_pe[j]))] = row

    plans: list[dict[str, Any]] = []
    for layer_id, layer in layers_out.items():
        seeds = list(layer.get("resample_seeds") or [])
        chord_cells = {
            (int(rec["i"]), int(rec["j"])) for rec in (layer.get("cell_lines") or [])
        }
        resampled = _resampled_cells(seeds, chord_cells, pad, n_ph, n_pe)
        if chord_cells & resampled:
            raise RuntimeError("A chord cell was placed in the resample set.")
        windows = _solid_windows(resampled)
        for i0, i1, j0, j1 in windows:
            for j in range(j0, j1):
                for i in range(i0, i1):
                    if (i, j) not in resampled:
                        raise RuntimeError(
                            f"Window pH {i0}–{i1}, pe {j0}–{j1} contains cell {(i, j)}, "
                            "which is not resampled."
                        )
        layer["skip_cells"] = [[i, j] for i, j in sorted(resampled)]
        layer["resample_windows"] = []
        stats.n_resample_cells += len(resampled)
        stats.n_resample_windows += len(windows)
        for i0, i1, j0, j1 in windows:
            plan = _window_work(
                i0, i1, j0, j1, resampled, sub, base_ph, base_pe, known
            )
            plan["layer_id"] = layer_id
            plans.append(plan)

    pending: dict[tuple[float, float], tuple[float, float]] = {}
    for plan in plans:
        for ph, pe in plan["missing"]:
            pending.setdefault(point_key(ph, pe), (ph, pe))
    if not plans:
        return
    print(
        f"[phaser trace] resample pad {pad} sub {sub}: "
        f"{stats.n_resample_cells} cells, {stats.n_resample_windows} windows, "
        f"{len(pending)} new points",
        flush=True,
    )
    if pending:
        from .sweep import run_point_sweep

        def _sweep_progress(done: int, total: int) -> None:
            _report(progress_cb, done, total, "resample")

        _report(progress_cb, 0, len(pending), "resample")
        fresh = run_point_sweep(
            params,
            list(pending.values()),
            compute_pool=compute_pool,
            job_id=job_id,
            progress_cb=_sweep_progress,
            progress_total=len(pending),
        )
        for row in fresh:
            known[point_key(float(row.ph), float(row.pe))] = trace_corner_payload(row)
            if not getattr(row, "synthetic_label", None):
                stats.n_resample_evals += 1

    workers = int(compute_pool.workers)
    fine_jobs: list[dict[str, Any]] = []
    window_meta: list[dict[str, Any]] = []
    for plan in plans:
        fine_ph = plan["fine_ph"]
        fine_pe = plan["fine_pe"]
        fine_ij: dict[tuple[int, int], dict] = {}
        cats: dict[tuple[int, int], str] = {}
        layer_id = plan["layer_id"]
        # Category labels for the mixed-cell test come from the swept rows.
        # The worker still root-finds; this pass only decides which fine cells
        # are mixed so a uniform neighbourhood does not take a trace slot.
        cat_fn = cat_fns.get(layer_id)
        if cat_fn is None:
            known_layers = sorted(cat_fns)
            raise LookupError(
                f"Layer {layer_id!r} is not produced by this trace. "
                f"Known layers: {known_layers}."
            )
        for fi, fj in plan["nodes"]:
            ph = float(fine_ph[fi])
            pe = float(fine_pe[fj])
            row = known.get(point_key(ph, pe))
            if row is None:
                raise RuntimeError(
                    f"No result for the fine-grid point at pH {ph:.4f}, pe {pe:.4f}."
                )
            payload = row if isinstance(row, dict) else trace_corner_payload(row)
            payload = dict(payload)
            payload["ph"] = ph
            payload["pe"] = pe
            fine_ij[(fi, fj)] = payload
            view = payload if isinstance(payload, dict) else ResultView(payload)
            cats[(fi, fj)] = cat_fn(view)
        fine_cells: list[tuple[int, int]] = []
        i0, j0 = int(plan["i0"]), int(plan["j0"])
        for i, j in plan["covered"]:
            fi0 = (i - i0) * sub
            fj0 = (j - j0) * sub
            for fj in range(fj0, fj0 + sub):
                for fi in range(fi0, fi0 + sub):
                    labels = {
                        cats[(fi, fj)],
                        cats[(fi + 1, fj)],
                        cats[(fi + 1, fj + 1)],
                        cats[(fi, fj + 1)],
                    }
                    if len(labels) > 1:
                        fine_cells.append((fi, fj))
        rows = [dict(fine_ij[key]) for key in sorted(fine_ij)]
        window_id = len(window_meta)
        window_meta.append(
            {
                "layer_id": layer_id,
                "fine_ph": [float(v) for v in fine_ph],
                "fine_pe": [float(v) for v in fine_pe],
                "rows": rows,
                "layer": _blank_layer(),
            }
        )
        if not fine_cells:
            continue
        fine_cfg = dict(trace_cfg)
        fine_cfg["base_ph"] = [float(v) for v in fine_ph]
        fine_cfg["base_pe"] = [float(v) for v in fine_pe]
        fine_cfg["resample"] = False
        chunks = _chunk_cells(fine_cells)
        added = _assign_trace_batches(chunks, workers, fine_ij)
        for job in added:
            job["cfg"] = fine_cfg
            job["layer_id"] = layer_id
            job["window"] = window_id
        fine_jobs.extend(added)

    ran_fine = bool(fine_jobs) or bool(pending)
    if not pending:
        # Every fine node was already on the coarse grid. The resampling
        # slice still completes so the bar moves on to the fine trace.
        _report(progress_cb, 1, 1, "resample")
    if fine_jobs:
        _collect_window_jobs(
            pool, fine_jobs, stats, window_meta, progress_cb, job_id
        )
    else:
        _report(progress_cb, 1, 1, "restrace")
    if ran_fine:
        _restore_coarse_axis(pool, workers, trace_cfg, job_id)

    by_layer: dict[str, list[dict[str, Any]]] = {}
    for meta in window_meta:
        by_layer.setdefault(meta["layer_id"], []).append(
            {
                "fine_ph": meta["fine_ph"],
                "fine_pe": meta["fine_pe"],
                "rows": meta["rows"],
                "layer": meta["layer"],
            }
        )
    for layer_id, windows in by_layer.items():
        layers_out[layer_id]["resample_windows"] = windows


def _category_fns(trace_cfg: dict[str, Any]) -> dict[str, Any]:
    """Category function per layer, rebuilt on the parent from the trace config.

    Workers hold the live specs. The parent only needs the label at each fine
    corner so it can skip uniform fine cells. Root-finding stays on the worker.
    """
    from .boundary_trace import _trace_mode_plugins

    params = grid_job_params_from_dict(trace_cfg["params"])
    specs, _resolve = _trace_mode_plugins(
        trace_cfg["trace_mode"], params, params.db_path
    )
    return {spec.layer_id: spec.cat_fn for spec in specs}
