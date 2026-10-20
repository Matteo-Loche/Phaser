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
from ..services.job_control import check_abort, iter_futures_abortable
from .engine import GridPointResult, point_key

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
    """Index rows by grid node without copying aqueous rankings."""
    from .engine import ResultView

    by_key: dict[tuple[float, float], dict] = {}
    for row in results:
        if isinstance(row, GridPointResult):
            stored = ResultView(row)
            phv, pev = float(row.ph), float(row.pe)
        else:
            stored = row
            phv, pev = float(row["ph"]), float(row["pe"])
        by_key[point_key(phv, pev)] = stored
    grid: list[list[dict | None]] = []
    for phv in ph:
        col: list[dict | None] = []
        for pev in pe:
            col.append(by_key.get(point_key(float(phv), float(pev))))
        grid.append(col)
    return grid


def _log_range_results(
    results: list[GridPointResult] | list[dict], key: str
) -> tuple[float, float] | None:
    """Finite log10(TOT) span, read in place. Synthetic points are skipped."""
    from .engine import ResultView

    vals: list[float] = []
    for row in results:
        if isinstance(row, GridPointResult):
            if row.synthetic_label:
                continue
            view = ResultView(row)
        else:
            if row.get("synthetic_label"):
                continue
            view = row
        log_t = log_tot_from_row(view, key)
        if log_t is not None:
            vals.append(log_t)
    if not vals:
        return None
    return min(vals), max(vals)


def _chunk_contour_cells(
    n_ph: int,
    n_pe: int,
    *,
    workers: int,
    bands_per_worker: int | None = None,
) -> list[list[tuple[int, int]]]:
    """Split the cell grid into one pool job per pH-line group.

    Each job is ``bands_per_worker`` consecutive pH-cell rows (default from
    ``CONTOUR_BANDS_PER_WORKER``), so progress can advance when a line group
    finishes. ``workers`` is unused for chunking (the pool caps concurrency);
    kept for call-site compatibility. Morton-style cell shards are avoided
    (isolines need locality; seams are re-stitched after merge).
    """
    del workers  # concurrency is enforced by the pool, not by chunk count
    n_i = max(int(n_ph) - 1, 0)
    n_j = max(int(n_pe) - 1, 0)
    if n_i == 0 or n_j == 0:
        return []
    rows_per_job = max(
        1,
        int(
            bands_per_worker
            if bands_per_worker is not None
            else config.CONTOUR_BANDS_PER_WORKER
        ),
    )
    strips: list[list[tuple[int, int]]] = []
    for start in range(0, n_i, rows_per_job):
        height = min(rows_per_job, n_i - start)
        strips.append(
            [
                (i, j)
                for i in range(start, start + height)
                for j in range(n_j)
            ]
        )
    return strips


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


def _axis_index(axis: np.ndarray, value: float) -> int | None:
    """Grid-node index when ``value`` lands on the axis, else None."""
    if axis.size == 0:
        return None
    slot = int(np.searchsorted(axis, value))
    if slot < axis.size and abs(float(axis[slot]) - value) <= 1e-8:
        return slot
    if slot > 0 and abs(float(axis[slot - 1]) - value) <= 1e-8:
        return slot - 1
    return None


class _SlabTotals:
    """One node's aqueous totals, read from the shared numeric block."""

    def __init__(self, values: np.ndarray, index: dict[str, int]) -> None:
        self._values = values
        self._index = index

    def get(self, key: str, default=None):
        slot = self._index.get(key)
        if slot is None:
            return default
        val = float(self._values[slot])
        if not math.isfinite(val) or val <= 0.0:
            return default
        return val


class _SlabCell:
    def __init__(self, converged: int, values: np.ndarray, index: dict[str, int]) -> None:
        self._converged = converged
        self._values = values
        self._index = index

    def get(self, key: str, default=None):
        if key == "converged":
            return bool(self._converged)
        if key == "synthetic_label":
            return None
        if key == "aq_total_by_key":
            return _SlabTotals(self._values, self._index)
        return default


class _SlabColumn:
    def __init__(
        self,
        totals: np.ndarray,
        converged: np.ndarray,
        index: dict[str, int],
        i: int,
    ) -> None:
        self._totals = totals
        self._converged = converged
        self._index = index
        self._i = i

    def __getitem__(self, j: int) -> _SlabCell:
        return _SlabCell(
            int(self._converged[self._i, j]),
            self._totals[:, self._i, j],
            self._index,
        )


class _SlabRows:
    """``rows[ph_index][pe_index]`` over the shared totals block."""

    def __init__(self, totals: np.ndarray, converged: np.ndarray, keys: tuple[str, ...]) -> None:
        self._totals = totals
        self._converged = converged
        self._index = {str(key): i for i, key in enumerate(keys)}

    def __getitem__(self, i: int) -> _SlabColumn:
        return _SlabColumn(self._totals, self._converged, self._index, i)


class _SlabEvaluator:
    """Grid nodes come from shared memory. Off-node samples call PHREEQC."""

    slab_ready = True

    def __init__(
        self,
        ph: np.ndarray,
        pe: np.ndarray,
        totals: np.ndarray,
        converged: np.ndarray,
        keys: tuple[str, ...],
    ) -> None:
        self._ph = ph
        self._pe = pe
        self._totals = totals
        self._converged = converged
        self._keys = keys
        self._index = {str(key): i for i, key in enumerate(keys)}

    def eval(self, ph: float, pe: float) -> dict:
        i = _axis_index(self._ph, float(ph))
        j = _axis_index(self._pe, float(pe))
        if i is not None and j is not None:
            totals: dict[str, float] = {}
            for key, slot in self._index.items():
                val = float(self._totals[slot, i, j])
                if math.isfinite(val) and val > 0.0:
                    totals[key] = val
            return {
                "ph": float(ph),
                "pe": float(pe),
                "converged": bool(self._converged[i, j]),
                "aq_total_by_key": totals,
            }
        from .engine import evaluate_point, worker_params, worker_phreeqc

        return asdict(
            evaluate_point(worker_phreeqc(), ph=float(ph), pe=float(pe), params=worker_params())
        )

    def eval_at_t(
        self, t: float, ph0: float, pe0: float, ph1: float, pe1: float
    ) -> dict:
        return self.eval(ph0 + t * (ph1 - ph0), pe0 + t * (pe1 - pe0))


_WORKER_SHM: list[Any] = []
_WORKER_CONTOUR_READY = False


def _attach_shared(name: str):
    """Attach a block the parent created without tracking it in this process.

    ``track=False`` keeps a killed worker from unlinking a block other workers
    are still reading. Linux may store the name with or without a leading
    slash; both are tried, and a miss raises.
    """
    from multiprocessing import shared_memory

    candidates = [name]
    if name.startswith("/"):
        candidates.append(name[1:])
    elif name:
        candidates.append("/" + name)
    last_error: FileNotFoundError | None = None
    for cand in dict.fromkeys(candidates):
        try:
            return shared_memory.SharedMemory(name=cand, track=False)
        except FileNotFoundError as exc:
            last_error = exc
    raise FileNotFoundError(f"Shared totals block {name!r} is not available") from last_error


def _publish_totals_slab(
    ph: np.ndarray,
    pe: np.ndarray,
    results: list[GridPointResult] | list[dict],
    keys: list[str],
):
    """Copy aqueous totals into one numeric block. Missing values stay NaN."""
    from multiprocessing import shared_memory

    n_ph = int(ph.size)
    n_pe = int(pe.size)
    totals = np.full((len(keys), n_ph, n_pe), np.nan, dtype=np.float64)
    converged = np.zeros((n_ph, n_pe), dtype=np.uint8)
    ph_lookup = {round(float(v), 12): i for i, v in enumerate(ph)}
    pe_lookup = {round(float(v), 12): j for j, v in enumerate(pe)}
    for row in results:
        # Synthetic water rows are left out on purpose. They have no aqueous
        # total, and filling that hole with a PHREEQC call would compute the
        # water mask. A node that was never evaluated looks the same here.
        if isinstance(row, GridPointResult):
            if row.synthetic_label:
                continue
            phv, pev = float(row.ph), float(row.pe)
            ok = bool(row.converged)
            aq = row.aq_total_by_key or {}
        else:
            if row.get("synthetic_label"):
                continue
            phv, pev = float(row["ph"]), float(row["pe"])
            ok = bool(row.get("converged", True))
            aq = row.get("aq_total_by_key") or {}
        i = ph_lookup.get(round(phv, 12))
        j = pe_lookup.get(round(pev, 12))
        if i is None or j is None:
            continue
        converged[i, j] = 1 if ok else 0
        if not ok:
            continue
        for slot, key in enumerate(keys):
            raw = aq.get(key)
            try:
                val = float(raw)
            except (TypeError, ValueError):
                continue
            if math.isfinite(val) and val > 0.0:
                totals[slot, i, j] = val
    totals_shm = shared_memory.SharedMemory(create=True, size=totals.nbytes)
    conv_shm = shared_memory.SharedMemory(create=True, size=converged.nbytes)
    np.ndarray(totals.shape, dtype=np.float64, buffer=totals_shm.buf)[:] = totals
    np.ndarray(converged.shape, dtype=np.uint8, buffer=conv_shm.buf)[:] = converged
    return totals_shm, conv_shm


def _release_slab(totals_shm, conv_shm) -> None:
    for shm in (totals_shm, conv_shm):
        if shm is None:
            continue
        try:
            shm.close()
        except (BufferError, OSError):
            pass
        try:
            shm.unlink()
        except FileNotFoundError:
            pass


def _install_contour_slab(cfg: dict[str, Any]) -> None:
    """Attach the shared totals block once on this worker."""
    global _WORKER_PH, _WORKER_PE, _WORKER_ROWS, _WORKER_EVALUATOR, _WORKER_TOL
    global _WORKER_PARAMS, _WORKER_CONTOUR_READY
    if _WORKER_CONTOUR_READY:
        return
    totals_shm = _attach_shared(str(cfg["totals_name"]))
    conv_shm = _attach_shared(str(cfg["conv_name"]))
    _WORKER_SHM[:] = [totals_shm, conv_shm]
    totals = np.ndarray(tuple(cfg["totals_shape"]), dtype=np.float64, buffer=totals_shm.buf)
    converged = np.ndarray(tuple(cfg["conv_shape"]), dtype=np.uint8, buffer=conv_shm.buf)
    keys = tuple(str(key) for key in cfg["keys"])
    _WORKER_PH = np.asarray(cfg["ph"], dtype=float)
    _WORKER_PE = np.asarray(cfg["pe"], dtype=float)
    _WORKER_ROWS = _SlabRows(totals, converged, keys)
    _WORKER_EVALUATOR = _SlabEvaluator(_WORKER_PH, _WORKER_PE, totals, converged, keys)
    _WORKER_TOL = float(cfg["tol"])
    from .engine import worker_params

    _WORKER_PARAMS = worker_params()
    _WORKER_CONTOUR_READY = True


def _arm_contour_worker(cfg: dict[str, Any]) -> bool:
    """One progress tick per worker as it attaches the totals block."""
    _install_contour_slab(cfg)
    return True


def _contour_chunk_job(job: dict[str, Any]) -> dict[str, Any]:
    _install_contour_slab(job["cfg"])
    assert _WORKER_PH is not None and _WORKER_PE is not None
    assert _WORKER_ROWS is not None and _WORKER_EVALUATOR is not None
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
        "by_level": {str(lv): segs for lv, segs in by_level.items() if segs},
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
    key_levels: list[tuple[str, list[float]]],
    tol: float,
    job_id: str | None,
    progress_cb: Callable[[int, int], None] | None,
    slab_cfg: dict[str, Any],
    compute_pool,
) -> dict[str, list[dict[str, Any]]]:
    workers = compute_pool.workers
    chunks = _chunk_contour_cells(int(ph.size), int(pe.size), workers=workers)
    jobs: list[dict[str, Any]] = []
    for key, levels in key_levels:
        if not levels:
            continue
        for chunk in chunks:
            jobs.append({"key": key, "levels": levels, "cells": chunk, "cfg": slab_cfg})
    if not jobs:
        if progress_cb:
            progress_cb(1, 1)
        return {}

    n_workers = max(int(workers), 1)
    total_units = n_workers + len(jobs)
    done_units = 0

    def report() -> None:
        if progress_cb:
            progress_cb(done_units, total_units)

    report()
    merged: dict[str, dict[float, list[list[list[float]]]]] = {}

    def _execute(pool) -> None:
        nonlocal done_units
        from ..services.job_control import hold_phase_for_test

        hold_phase_for_test(job_id, "lines")
        warmups = {pool.submit(_arm_contour_worker, slab_cfg): i for i in range(n_workers)}
        for future, _idx in iter_futures_abortable(warmups, job_id=job_id):
            future.result(timeout=0.1)
            done_units += 1
            report()
        futures = {
            pool.submit(_contour_chunk_job, job): idx for idx, job in enumerate(jobs)
        }
        for future, _idx in iter_futures_abortable(futures, job_id=job_id):
            result = future.result(timeout=0.1)
            key = result["key"]
            bucket = merged.setdefault(key, {})
            for lv_s, segs in (result.get("by_level") or {}).items():
                bucket.setdefault(float(lv_s), []).extend(segs)
            done_units += 1
            report()

    try:
        _execute(compute_pool.pool)
    except Exception as exc:
        from ..services.job_control import JobAborted

        if isinstance(exc, JobAborted):
            raise
        check_abort(job_id)
        raise RuntimeError(
            f"Totals-contour worker pool failed ({type(exc).__name__}): {exc}"
        ) from exc

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
    compute_pool=None,
) -> dict[str, Any]:
    """Trace ``log10(TOT)`` isolines for each key.

    When ``params`` is set (live PHREEQC path), contiguous pH-line bands run on
    the job pool. Workers read one shared totals block and call PHREEQC only for
    samples that fall between grid nodes. Synthetic / analytic evaluators stay
    single-process.
    """
    step = clamp_contour_log_step(
        log_step if log_step is not None else config.TOTALS_CONTOUR_LOG_STEP_DEFAULT
    )
    brent_tol = float(
        tol if tol is not None else config.BOUNDARY_TRACE_TOLERANCE
    )
    ph = np.asarray(ph, dtype=float)
    pe = np.asarray(pe, dtype=float)

    key_levels: list[tuple[str, list[float]]] = []
    for key in keys:
        rng = _log_range_results(results, str(key))
        if rng is None:
            continue
        levels = contour_log_levels(rng[0], rng[1], step)
        if levels:
            key_levels.append((str(key), levels))

    if not key_levels:
        if progress_cb:
            progress_cb(1, 1)
        return {"units": "mol/kgw", "log_step": step, "by_key": {}}

    use_pool = params is not None and not isinstance(evaluator, AnalyticTotEvaluator)

    if use_pool:
        from .session import require_compute_pool

        compute_pool = require_compute_pool(compute_pool)
        key_names = [key for key, _levels in key_levels]
        totals_shm, conv_shm = _publish_totals_slab(ph, pe, results, key_names)
        slab_cfg = {
            "totals_name": totals_shm.name,
            "conv_name": conv_shm.name,
            "totals_shape": (len(key_names), int(ph.size), int(pe.size)),
            "conv_shape": (int(ph.size), int(pe.size)),
            "keys": key_names,
            "ph": ph.tolist(),
            "pe": pe.tolist(),
            "tol": brent_tol,
        }
        try:
            by_key = _find_totals_contours_parallel(
                ph=ph,
                pe=pe,
                key_levels=key_levels,
                tol=brent_tol,
                job_id=job_id,
                progress_cb=progress_cb,
                slab_cfg=slab_cfg,
                compute_pool=compute_pool,
            )
        finally:
            _release_slab(totals_shm, conv_shm)
    else:
        rows = _seed_grid(ph, pe, results)
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
