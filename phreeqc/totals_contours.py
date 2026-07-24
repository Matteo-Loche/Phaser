"""Root-find aqueous master-total (TOT) isolines on a mineral-stability grid.

Contours are ``log10(TOT(key)) = level`` in mol/kgw. Levels are spaced by
``log_step`` (default 2) between the finite field min/max on the base grid.
Edge zeros use the same ``brentq`` pattern as phase-boundary tracing.
"""
from __future__ import annotations

import math
from dataclasses import asdict
from typing import Any, Callable, Protocol

import numpy as np
from scipy.optimize import brentq

from .. import config
from ..services.job_control import check_abort, iter_futures_abortable, managed_process_pool
from .engine import GridPointResult, grid_job_params_from_dict, point_key

_TOT_FLOOR = 1e-30
_EDGE_CORNERS = (
    ((0, 0), (1, 0)),  # bottom: (i,j) -> (i+1,j)
    ((1, 0), (1, 1)),  # right
    ((1, 1), (0, 1)),  # top
    ((0, 1), (0, 0)),  # left
)

# Process-pool worker state (spawn initializer).
_WORKER_PARAMS = None
_WORKER_PH: np.ndarray | None = None
_WORKER_PE: np.ndarray | None = None
_WORKER_ROWS: list[list[dict | None]] | None = None
_WORKER_EVALUATOR: TotalsEval | None = None
_WORKER_TOL: float = 1e-3


class TotalsEval(Protocol):
    def eval(self, ph: float, pe: float) -> dict: ...

    def eval_at_t(
        self, t: float, ph0: float, pe0: float, ph1: float, pe1: float
    ) -> dict: ...


class AnalyticTotEvaluator:
    """Synthetic evaluator for unit tests (no PHREEQC)."""

    def __init__(self, tot_fn: Callable[[float, float], float], key: str = "Fe(2)"):
        self._tot_fn = tot_fn
        self._key = key

    def eval(self, ph: float, pe: float) -> dict:
        tot = float(self._tot_fn(ph, pe))
        return {
            "ph": ph,
            "pe": pe,
            "converged": tot > 0.0 and math.isfinite(tot),
            "aq_total_by_key": {self._key: tot} if tot > 0.0 else {},
        }

    def eval_at_t(
        self, t: float, ph0: float, pe0: float, ph1: float, pe1: float
    ) -> dict:
        ph = ph0 + t * (ph1 - ph0)
        pe = pe0 + t * (pe1 - pe0)
        return self.eval(ph, pe)


def clamp_contour_log_step(step: float | None) -> float:
    raw = float(
        step if step is not None else config.TOTALS_CONTOUR_LOG_STEP_DEFAULT
    )
    lo = float(config.TOTALS_CONTOUR_LOG_STEP_MIN)
    hi = float(config.TOTALS_CONTOUR_LOG_STEP_MAX)
    if not math.isfinite(raw) or raw <= 0:
        raw = float(config.TOTALS_CONTOUR_LOG_STEP_DEFAULT)
    return min(hi, max(lo, raw))


def contour_log_levels(min_log: float, max_log: float, step: float) -> list[float]:
    """Inclusive levels from floor(min/step)*step to ceil(max/step)*step."""
    step = clamp_contour_log_step(step)
    if not (math.isfinite(min_log) and math.isfinite(max_log)):
        return []
    if max_log < min_log:
        min_log, max_log = max_log, min_log
    start = math.floor(min_log / step) * step
    end = math.ceil(max_log / step) * step
    # Guard float drift
    n = int(round((end - start) / step)) + 1
    levels = [round(start + i * step, 10) for i in range(max(n, 0))]
    return [lv for lv in levels if min_log - 1e-9 <= lv <= max_log + 1e-9]


def log_tot_from_row(row: dict, key: str, *, floor: float = _TOT_FLOOR) -> float | None:
    """Return log10(TOT) or None when missing / non-converged / non-positive."""
    if not row.get("converged", True):
        return None
    totals = row.get("aq_total_by_key") or {}
    tot = totals.get(key)
    if tot is None:
        return None
    try:
        val = float(tot)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(val) or val <= 0.0:
        return None
    return math.log10(max(val, floor))


def level_scalar(key: str, level: float, *, floor: float = _TOT_FLOOR) -> Callable[[dict], float]:
    """Scalar ``log10(TOT) - level`` for brentq (NaN-safe via large magnitude)."""

    def _fn(row: dict) -> float:
        log_t = log_tot_from_row(row, key, floor=floor)
        if log_t is None:
            # Push away from zero so lost corners do not false-bracket.
            return 1e6
        return log_t - level

    return _fn


def _edge_crossing(
    evaluator: TotalsEval,
    scalar_fn: Callable[[dict], float],
    ph0: float,
    pe0: float,
    ph1: float,
    pe1: float,
    *,
    tol: float,
) -> tuple[float, float] | None:
    f0 = scalar_fn(evaluator.eval(ph0, pe0))
    f1 = scalar_fn(evaluator.eval(ph1, pe1))
    if not (math.isfinite(f0) and math.isfinite(f1)):
        return None
    if f0 == 0.0:
        return ph0, pe0
    if f1 == 0.0:
        return ph1, pe1
    if f0 * f1 > 0:
        return None

    def f(t: float) -> float:
        return scalar_fn(evaluator.eval_at_t(t, ph0, pe0, ph1, pe1))

    try:
        t = float(brentq(f, 0.0, 1.0, xtol=tol, rtol=tol))
    except (ValueError, RuntimeError):
        return None
    return ph0 + t * (ph1 - ph0), pe0 + t * (pe1 - pe0)


def _field_log_range(
    rows_by_ij: list[list[dict | None]], key: str
) -> tuple[float, float] | None:
    vals: list[float] = []
    for row_list in rows_by_ij:
        for row in row_list:
            if not row or row.get("synthetic_label"):
                continue
            log_t = log_tot_from_row(row, key)
            if log_t is not None:
                vals.append(log_t)
    if not vals:
        return None
    return min(vals), max(vals)


def _stitch_snap_tol(ph: np.ndarray, pe: np.ndarray, *, tol: float) -> float:
    """Endpoint match tolerance: brentq noise + a fraction of cell size."""
    dph = float(np.min(np.abs(np.diff(ph)))) if ph.size > 1 else 1.0
    dpe = float(np.min(np.abs(np.diff(pe)))) if pe.size > 1 else 1.0
    cell = min(dph, dpe)
    if not math.isfinite(cell) or cell <= 0:
        cell = 1.0
    return max(float(tol) * 10.0, 1e-5, 0.05 * cell)


def _seed_grid(
    ph: np.ndarray,
    pe: np.ndarray,
    results: list[GridPointResult] | list[dict],
) -> list[list[dict | None]]:
    by_key: dict[tuple[float, float], dict] = {}
    for row in results:
        if isinstance(row, GridPointResult):
            d = asdict(row)
        else:
            d = dict(row)
        by_key[point_key(float(d["ph"]), float(d["pe"]))] = d
    grid: list[list[dict | None]] = []
    for i, phv in enumerate(ph):
        col: list[dict | None] = []
        for j, pev in enumerate(pe):
            col.append(by_key.get(point_key(float(phv), float(pev))))
        grid.append(col)
    return grid


def _light_seed_rows(results: list[GridPointResult] | list[dict]) -> list[dict]:
    """Compact seed payload for process-pool workers."""
    out: list[dict] = []
    for row in results:
        d = asdict(row) if isinstance(row, GridPointResult) else dict(row)
        out.append(
            {
                "ph": float(d["ph"]),
                "pe": float(d["pe"]),
                "converged": bool(d.get("converged", True)),
                "aq_total_by_key": dict(d.get("aq_total_by_key") or {}),
                "synthetic_label": d.get("synthetic_label"),
            }
        )
    return out


def _chunk_contour_cells(
    n_ph: int,
    n_pe: int,
    *,
    workers: int,
    bands_per_worker: int | None = None,
) -> list[list[tuple[int, int]]]:
    """Split the cell grid into one pool job per worker (pH-line bands).

    Build ``workers × bands_per_worker`` contiguous pH-line strips, then
    round-robin them so each worker owns that many (possibly non-adjacent)
    bands in a single future. Seed TOT values are trusted in workers — no
    corner re-PHREEQC. Morton-style cell shards are avoided (isolines need
    locality; seams are re-stitched after merge).
    """
    n_i = max(int(n_ph) - 1, 0)
    n_j = max(int(n_pe) - 1, 0)
    if n_i == 0 or n_j == 0:
        return []
    w = max(int(workers), 1)
    bpw = max(
        1,
        int(
            bands_per_worker
            if bands_per_worker is not None
            else config.CONTOUR_BANDS_PER_WORKER
        ),
    )
    n_bands = max(1, min(w * bpw, n_i))
    base, extra = divmod(n_i, n_bands)
    strips: list[list[tuple[int, int]]] = []
    start = 0
    for _bi in range(n_bands):
        height = base + (1 if _bi < extra else 0)
        if height <= 0:
            continue
        strips.append(
            [
                (i, j)
                for i in range(start, start + height)
                for j in range(n_j)
            ]
        )
        start += height
    n_jobs = min(w, len(strips))
    chunks: list[list[tuple[int, int]]] = [[] for _ in range(n_jobs)]
    for bi, strip in enumerate(strips):
        chunks[bi % n_jobs].extend(strip)
    return [c for c in chunks if c]


def _canonical_grid_edge(
    i0: int, j0: int, i1: int, j1: int
) -> tuple[tuple[int, int], tuple[int, int]]:
    a, b = (i0, j0), (i1, j1)
    return (a, b) if a <= b else (b, a)


def _stitch_polylines(
    segments: list[list[list[float]]],
    *,
    tol: float = 1e-5,
) -> list[list[list[float]]]:
    """Merge short chords into continuous polylines by matching endpoints."""
    chords: list[list[list[float]]] = []
    for seg in segments:
        if not seg or len(seg) < 2:
            continue
        # Normalize to open polylines (keep all vertices if already stitched).
        pts = [[float(p[0]), float(p[1])] for p in seg if p and len(p) >= 2]
        if len(pts) >= 2:
            chords.append(pts)
    if not chords:
        return []

    tol2 = tol * tol

    def close(a: list[float], b: list[float]) -> bool:
        return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 <= tol2

    unused = list(range(len(chords)))
    polylines: list[list[list[float]]] = []
    while unused:
        idx = unused.pop(0)
        poly = list(chords[idx])
        changed = True
        while changed:
            changed = False
            for k, ui in enumerate(list(unused)):
                other = chords[ui]
                if close(poly[-1], other[0]):
                    poly.extend(other[1:])
                elif close(poly[-1], other[-1]):
                    poly.extend(reversed(other[:-1]))
                elif close(poly[0], other[-1]):
                    poly = list(other) + poly[1:]
                elif close(poly[0], other[0]):
                    poly = list(reversed(other)) + poly[1:]
                else:
                    continue
                unused.pop(k)
                changed = True
                break
        # Drop near-duplicate consecutive vertices
        cleaned: list[list[float]] = [poly[0]]
        for p in poly[1:]:
            if not close(cleaned[-1], p):
                cleaned.append(p)
        if len(cleaned) >= 2:
            polylines.append(cleaned)
    return polylines


def _segments_for_cells(
    *,
    ph: np.ndarray,
    pe: np.ndarray,
    rows: list[list[dict | None]],
    cells: list[tuple[int, int]],
    key: str,
    levels: list[float],
    evaluator: TotalsEval,
    tol: float,
    job_id: str | None = None,
) -> dict[float, list[list[list[float]]]]:
    """Root-find contour chords for ``cells``; map level → stitched polylines.

    Shared grid edges are rooted once so adjacent cells share identical endpoints.
    """
    by_level: dict[float, list[list[list[float]]]] = {float(lv): [] for lv in levels}
    # level -> canonical edge -> crossing point (or None if no root)
    edge_cache: dict[float, dict[tuple[tuple[int, int], tuple[int, int]], list[float] | None]] = {
        float(lv): {} for lv in levels
    }

    def crossing_on_edge(
        level: float, i0: int, j0: int, i1: int, j1: int
    ) -> list[float] | None:
        cache = edge_cache[float(level)]
        ekey = _canonical_grid_edge(i0, j0, i1, j1)
        if ekey in cache:
            return cache[ekey]
        ph0 = float(ph[i0])
        pe0 = float(pe[j0])
        ph1 = float(ph[i1])
        pe1 = float(pe[j1])
        r0 = rows[i0][j0]
        r1 = rows[i1][j1]
        if r0 and r1:
            l0 = log_tot_from_row(r0, key)
            l1 = log_tot_from_row(r1, key)
            if l0 is None or l1 is None:
                cache[ekey] = None
                return None
            if (l0 - level) * (l1 - level) > 0 and l0 != level and l1 != level:
                cache[ekey] = None
                return None
        pt = _edge_crossing(
            evaluator,
            level_scalar(key, level),
            ph0,
            pe0,
            ph1,
            pe1,
            tol=tol,
        )
        out = [float(pt[0]), float(pt[1])] if pt is not None else None
        cache[ekey] = out
        return out

    for i, j in cells:
        if job_id and ((i + j) & 15) == 0:
            check_abort(job_id)
        for level in levels:
            crossings: list[list[float]] = []
            for (di0, dj0), (di1, dj1) in _EDGE_CORNERS:
                pt = crossing_on_edge(level, i + di0, j + dj0, i + di1, j + dj1)
                if pt is not None:
                    crossings.append(pt)
            segs: list[list[list[float]]] = []
            if len(crossings) == 2:
                segs.append(crossings)
            elif len(crossings) >= 4:
                # Ambiguous saddle: pair in edge-walk order.
                segs.append([crossings[0], crossings[1]])
                segs.append([crossings[2], crossings[3]])
            # Do not water-band clip here: clipping moves endpoints off the shared
            # grid-edge zeros and prevents stitching into continuous isolines.
            # The UI white O₂/H₂ masks cut strokes at the true gas lines.
            if segs:
                by_level[float(level)].extend(segs)

    snap = _stitch_snap_tol(ph, pe, tol=tol)
    for lv in list(by_level.keys()):
        by_level[lv] = _stitch_polylines(by_level[lv], tol=snap)
    return by_level


def _contour_worker_init(
    dll_path: str,
    db_path: str,
    params_dict: dict,
    ph: list[float],
    pe: list[float],
    seed_rows: list[dict],
    tol: float,
) -> None:
    global _WORKER_PARAMS, _WORKER_PH, _WORKER_PE, _WORKER_ROWS
    global _WORKER_EVALUATOR, _WORKER_TOL
    from .boundary_trace import PointEvaluator

    _WORKER_PARAMS = grid_job_params_from_dict(params_dict)
    _WORKER_PH = np.asarray(ph, dtype=float)
    _WORKER_PE = np.asarray(pe, dtype=float)
    _WORKER_ROWS = _seed_grid(_WORKER_PH, _WORKER_PE, seed_rows)
    seed_map = {
        (round(float(r["ph"]), 12), round(float(r["pe"]), 12)): r for r in seed_rows
    }
    # Seeds already punch aq_total_by_key with this job's params — trust them.
    _WORKER_EVALUATOR = PointEvaluator(
        _WORKER_PARAMS, seed_map, trust_seed_cache=True
    )
    _WORKER_TOL = float(tol)


def _contour_chunk_job(job: dict[str, Any]) -> dict[str, Any]:
    assert _WORKER_PH is not None and _WORKER_PE is not None
    assert _WORKER_ROWS is not None and _WORKER_EVALUATOR is not None
    assert _WORKER_PARAMS is not None
    by_level = _segments_for_cells(
        ph=_WORKER_PH,
        pe=_WORKER_PE,
        rows=_WORKER_ROWS,
        cells=[(int(i), int(j)) for i, j in job["cells"]],
        key=str(job["key"]),
        levels=[float(lv) for lv in job["levels"]],
        evaluator=_WORKER_EVALUATOR,
        tol=_WORKER_TOL,
    )
    return {
        "key": str(job["key"]),
        "by_level": {
            str(lv): segs for lv, segs in by_level.items() if segs
        },
    }


def _find_totals_contours_serial(
    *,
    ph: np.ndarray,
    pe: np.ndarray,
    rows: list[list[dict | None]],
    key_levels: list[tuple[str, list[float]]],
    evaluator: TotalsEval,
    tol: float,
    job_id: str | None,
    progress_cb: Callable[[int, int], None] | None,
) -> dict[str, list[dict[str, Any]]]:
    n_keys = max(len(key_levels), 1)
    if progress_cb:
        progress_cb(0, n_keys)
    by_key: dict[str, list[dict[str, Any]]] = {}
    cells = [
        (i, j)
        for i in range(max(int(ph.size) - 1, 0))
        for j in range(max(int(pe.size) - 1, 0))
    ]
    for ki, (key, levels) in enumerate(key_levels):
        if job_id:
            check_abort(job_id)
        if not levels:
            if progress_cb:
                progress_cb(ki + 1, n_keys)
            continue
        by_level = _segments_for_cells(
            ph=ph,
            pe=pe,
            rows=rows,
            cells=cells,
            key=key,
            levels=levels,
            evaluator=evaluator,
            tol=tol,
            job_id=job_id,
        )
        entries = [
            {"level": float(lv), "segments": by_level[float(lv)]}
            for lv in levels
            if by_level.get(float(lv))
        ]
        if entries:
            by_key[key] = entries
        if progress_cb:
            progress_cb(ki + 1, n_keys)
    return by_key


def _find_totals_contours_parallel(
    *,
    ph: np.ndarray,
    pe: np.ndarray,
    results: list[GridPointResult] | list[dict],
    key_levels: list[tuple[str, list[float]]],
    params: Any,
    tol: float,
    job_id: str | None,
    progress_cb: Callable[[int, int], None] | None,
    max_workers: int,
) -> dict[str, list[dict[str, Any]]]:
    chunks = _chunk_contour_cells(int(ph.size), int(pe.size), workers=max_workers)
    jobs: list[dict[str, Any]] = []
    for key, levels in key_levels:
        if not levels:
            continue
        for chunk in chunks:
            jobs.append({"key": key, "levels": levels, "cells": chunk})
    if not jobs:
        return {}
    if progress_cb:
        progress_cb(0, len(jobs))

    seed_rows = _light_seed_rows(results)
    merged: dict[str, dict[float, list[list[list[float]]]]] = {}
    with managed_process_pool(
        job_id,
        max_workers=max_workers,
        initializer=_contour_worker_init,
        initargs=(
            params.dll_path,
            params.db_path,
            asdict(params),
            ph.tolist(),
            pe.tolist(),
            seed_rows,
            tol,
        ),
    ) as pool:
        try:
            futures = {
                pool.submit(_contour_chunk_job, job): idx
                for idx, job in enumerate(jobs)
            }
            done = 0
            for future, _idx in iter_futures_abortable(futures, job_id=job_id):
                result = future.result(timeout=0.1)
                key = result["key"]
                bucket = merged.setdefault(key, {})
                for lv_s, segs in (result.get("by_level") or {}).items():
                    bucket.setdefault(float(lv_s), []).extend(segs)
                done += 1
                if progress_cb:
                    progress_cb(done, len(jobs))
        except Exception as exc:
            from ..services.job_control import JobAborted

            if isinstance(exc, JobAborted):
                raise
            check_abort(job_id)
            raise RuntimeError(
                f"Totals-contour worker pool failed ({type(exc).__name__}): {exc}"
            ) from exc

    # Re-stitch after merging worker bands (shared edges across band bounds).
    snap = _stitch_snap_tol(ph, pe, tol=tol)
    by_key: dict[str, list[dict[str, Any]]] = {}
    for key, levels in key_levels:
        bucket = merged.get(key) or {}
        entries = []
        for lv in levels:
            segs = bucket.get(float(lv)) or []
            if not segs:
                continue
            stitched = _stitch_polylines(segs, tol=snap)
            if stitched:
                entries.append({"level": float(lv), "segments": stitched})
        if entries:
            by_key[key] = entries
    return by_key


def find_totals_contours(
    *,
    ph: np.ndarray,
    pe: np.ndarray,
    results: list[GridPointResult] | list[dict],
    keys: tuple[str, ...] | list[str],
    log_step: float | None = None,
    evaluator: TotalsEval | None = None,
    tol: float | None = None,
    job_id: str | None = None,
    progress_cb: Callable[[int, int], None] | None = None,
    params: Any | None = None,
    max_workers: int | None = None,
) -> dict[str, Any]:
    """Trace ``log10(TOT)`` isolines for each key.

    When ``params`` is set (live PHREEQC path), contiguous pH-line bands are
    evaluated in a ``ProcessPoolExecutor`` (trusted seed TOT cache;
    ``CONTOUR_BANDS_PER_WORKER`` interleaved bands per worker — not Morton
    shards). Synthetic / analytic evaluators stay single-process.
    """
    step = clamp_contour_log_step(
        log_step if log_step is not None else config.TOTALS_CONTOUR_LOG_STEP_DEFAULT
    )
    brent_tol = float(
        tol if tol is not None else config.BOUNDARY_TRACE_TOLERANCE
    )
    ph = np.asarray(ph, dtype=float)
    pe = np.asarray(pe, dtype=float)
    rows = _seed_grid(ph, pe, results)

    key_levels: list[tuple[str, list[float]]] = []
    for key in keys:
        rng = _field_log_range(rows, key)
        if rng is None:
            continue
        levels = contour_log_levels(rng[0], rng[1], step)
        if levels:
            key_levels.append((str(key), levels))

    workers = int(max_workers if max_workers is not None else config.MAX_WORKERS)
    use_pool = (
        params is not None
        and workers > 1
        and not isinstance(evaluator, AnalyticTotEvaluator)
        and any(levels for _, levels in key_levels)
    )

    if use_pool:
        by_key = _find_totals_contours_parallel(
            ph=ph,
            pe=pe,
            results=results,
            key_levels=key_levels,
            params=params,
            tol=brent_tol,
            job_id=job_id,
            progress_cb=progress_cb,
            max_workers=workers,
        )
    else:
        if evaluator is None:
            evaluator = _SeedOnlyEvaluator(rows, ph, pe)
        by_key = _find_totals_contours_serial(
            ph=ph,
            pe=pe,
            rows=rows,
            key_levels=key_levels,
            evaluator=evaluator,
            tol=brent_tol,
            job_id=job_id,
            progress_cb=progress_cb,
        )

    return {
        "units": "mol/kgw",
        "log_step": step,
        "by_key": by_key,
    }


class _SeedOnlyEvaluator:
    """Nearest-seed evaluator when no PHREEQC PointEvaluator is supplied."""

    def __init__(
        self,
        rows: list[list[dict | None]],
        ph: np.ndarray,
        pe: np.ndarray,
    ):
        self._rows = rows
        self._ph = ph
        self._pe = pe

    def eval(self, ph: float, pe: float) -> dict:
        # Bilinear blend of aq_total_by_key from surrounding cell when possible.
        i = int(np.clip(np.searchsorted(self._ph, ph) - 1, 0, len(self._ph) - 2))
        j = int(np.clip(np.searchsorted(self._pe, pe) - 1, 0, len(self._pe) - 2))
        ph0, ph1 = float(self._ph[i]), float(self._ph[i + 1])
        pe0, pe1 = float(self._pe[j]), float(self._pe[j + 1])
        tx = 0.0 if ph1 == ph0 else (ph - ph0) / (ph1 - ph0)
        ty = 0.0 if pe1 == pe0 else (pe - pe0) / (pe1 - pe0)
        corners = (
            self._rows[i][j],
            self._rows[i + 1][j],
            self._rows[i + 1][j + 1],
            self._rows[i][j + 1],
        )
        keys: set[str] = set()
        for c in corners:
            if c:
                keys.update((c.get("aq_total_by_key") or {}).keys())
        totals: dict[str, float] = {}
        for key in keys:
            vals = []
            weights = []
            wts = ((1 - tx) * (1 - ty), tx * (1 - ty), tx * ty, (1 - tx) * ty)
            for c, w in zip(corners, wts):
                if not c:
                    continue
                log_t = log_tot_from_row(c, key)
                if log_t is None:
                    continue
                vals.append(10**log_t)
                weights.append(w)
            if vals and sum(weights) > 0:
                totals[key] = float(sum(v * w for v, w in zip(vals, weights)) / sum(weights))
        return {
            "ph": ph,
            "pe": pe,
            "converged": bool(totals),
            "aq_total_by_key": totals,
        }

    def eval_at_t(
        self, t: float, ph0: float, pe0: float, ph1: float, pe1: float
    ) -> dict:
        return self.eval(ph0 + t * (ph1 - ph0), pe0 + t * (pe1 - pe0))
