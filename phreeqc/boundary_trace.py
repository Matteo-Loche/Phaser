"""Boundary tracing via 1D/2D root finding on mixed base cells.

Clean 2-category cells use edge ``brentq`` and a single dividing line. 3-category
cells emit convex fill regions bounded by oriented lines: a genuine triple point
(three crossings) or a diagonal band (four crossings). 2-category saddles (four
edge crossings) split on two crossing lines. Unresolved 4-category cells and
lost brackets share one sampled sub-grid per cell. Stability limits (converged
vs failed) are traced separately. Solid/aqueous category pairs that share a
name are disambiguated upstream via the ``(s)`` suffix (see ``diagram.packer``).
"""
from __future__ import annotations

import math
import os
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, fields, replace
from typing import Any, Callable

import numpy as np
from scipy.optimize import brentq, least_squares, root
from skimage.measure import find_contours

from .. import config
from ..diagram.packer import label_is_solid, phase_from_label, subset_key
from .engine import GridJobParams, GridPointResult, evaluate_point, grid_job_params_from_dict, init_phreeqc
from .sweep import _point_key

# Floor for absent aqueous species so log(m_A)-log(m_B) brackets at dominance edges
# even when a species is below top-N at a far cell corner.
_MOL_FLOOR = 1e-30

_WORKER_PQ = None


@dataclass
class LayerSpec:
    layer_id: str
    cat_fn: Callable[[dict], str]


@dataclass
class TraceStats:
    n_trace_evals: int = 0
    n_fallback_evals: int = 0
    n_crossings: int = 0
    n_cells_traced: int = 0
    n_cells_fallback: int = 0
    n_segments: int = 0
    n_brentq_si: int = 0
    n_brentq_aq: int = 0
    n_brentq_conv: int = 0
    n_stability_segments: int = 0
    n_cells_complex_fallback: int = 0
    n_crossing_cache_hits: int = 0
    n_cells_triple_traced: int = 0
    n_cells_saddle_traced: int = 0
    n_brentq_2d: int = 0
    n_gas_segments: int = 0


_CROSSING_UNCACHED = object()


def _edge_grid_nodes(i: int, j: int, edge: int) -> tuple[tuple[int, int], tuple[int, int]]:
    """Grid-node indices (ph_idx, pe_idx) at the start and end of a cell edge."""
    corners = ((i, j), (i + 1, j), (i + 1, j + 1), (i, j + 1))
    return corners[edge], corners[(edge + 1) % 4]


def _canonical_edge_crossing_key(
    i: int, j: int, edge: int, cat_a: str, cat_b: str
) -> tuple[tuple[int, int], tuple[int, int], str, str, bool]:
    """Cache key for a physical grid edge and category pair.

    Returns ``(n_lo, n_hi, lo_cat, hi_cat, forward)`` where ``n_lo <= n_hi``
    lexicographically and *forward* is True when the local edge direction runs
    from ``n_lo`` to ``n_hi`` (so ``t_local == t_canonical``).
    """
    n0, n1 = _edge_grid_nodes(i, j, edge)
    lo_c, hi_c = sorted((cat_a, cat_b))
    if n0 <= n1:
        return n0, n1, lo_c, hi_c, True
    return n1, n0, lo_c, hi_c, False


def _canonical_convergence_key(
    i: int, j: int, edge: int
) -> tuple[tuple[int, int], tuple[int, int], bool]:
    """Canonical node-pair key for converged↔failed edge crossings."""
    n0, n1 = _edge_grid_nodes(i, j, edge)
    if n0 <= n1:
        return n0, n1, True
    return n1, n0, False


def _merge_stats(dest: TraceStats, src: TraceStats) -> None:
    for f in fields(TraceStats):
        setattr(dest, f.name, getattr(dest, f.name) + getattr(src, f.name))


class PointEvaluator:
    """Cached on-demand PHREEQC evaluation."""

    def __init__(self, params: GridJobParams, seed_rows: dict[tuple[float, float], dict]):
        self._params = params
        self._cache: dict[tuple[float, float], dict] = dict(seed_rows)
        self._full: set[tuple[float, float]] = set()
        self.n_evals = 0
        self._pq = init_phreeqc(params.dll_path, params.db_path)
        self._crossing_t: dict[
            tuple[tuple[int, int], tuple[int, int], str, str], float | None
        ] = {}
        self._convergence_crossing_t: dict[
            tuple[tuple[int, int], tuple[int, int]], float | None
        ] = {}
        # Solid phases identify solid<->aqueous edges (where the aqueous side is
        # labelled by its dominant species, not the literal string "aqueous").
        self.solid_phases: frozenset[str] = frozenset(params.phases)
        # Phase names shared with an aqueous species; their solid form is the
        # "(s)"-suffixed label, so a bare name always denotes the aqueous side.
        self.collisions: frozenset[str] = frozenset(params.solid_aqueous_collisions)

    def crossing_t_lookup(
        self, i: int, j: int, edge: int, cat_a: str, cat_b: str
    ) -> float | None | object:
        n_lo, n_hi, lo_c, hi_c, forward = _canonical_edge_crossing_key(
            i, j, edge, cat_a, cat_b
        )
        key = (n_lo, n_hi, lo_c, hi_c)
        if key not in self._crossing_t:
            return _CROSSING_UNCACHED
        t_canon = self._crossing_t[key]
        if t_canon is None:
            return None
        return t_canon if forward else 1.0 - t_canon

    def store_crossing_t(
        self,
        i: int,
        j: int,
        edge: int,
        cat_a: str,
        cat_b: str,
        t: float | None,
    ) -> None:
        n_lo, n_hi, lo_c, hi_c, forward = _canonical_edge_crossing_key(
            i, j, edge, cat_a, cat_b
        )
        key = (n_lo, n_hi, lo_c, hi_c)
        if t is None:
            t_canon = None
        elif forward:
            t_canon = t
        else:
            t_canon = 1.0 - t
        self._crossing_t[key] = t_canon

    def convergence_crossing_t_lookup(
        self, i: int, j: int, edge: int
    ) -> float | None | object:
        n_lo, n_hi, forward = _canonical_convergence_key(i, j, edge)
        key = (n_lo, n_hi)
        if key not in self._convergence_crossing_t:
            return _CROSSING_UNCACHED
        t_canon = self._convergence_crossing_t[key]
        if t_canon is None:
            return None
        return t_canon if forward else 1.0 - t_canon

    def store_convergence_crossing_t(
        self, i: int, j: int, edge: int, t: float | None
    ) -> None:
        n_lo, n_hi, forward = _canonical_convergence_key(i, j, edge)
        key = (n_lo, n_hi)
        if t is None:
            t_canon = None
        elif forward:
            t_canon = t
        else:
            t_canon = 1.0 - t
        self._convergence_crossing_t[key] = t_canon

    def eval(self, ph: float, pe: float) -> dict:
        key = _point_key(ph, pe)
        if key not in self._cache or key not in self._full:
            row = evaluate_point(self._pq, ph=ph, pe=pe, params=self._params)
            self._cache[key] = asdict(row)
            self._full.add(key)
            self.n_evals += 1
        return self._cache[key]

    def eval_at_t(
        self, t: float, ph0: float, pe0: float, ph1: float, pe1: float
    ) -> dict:
        ph = ph0 + t * (ph1 - ph0)
        pe = pe0 + t * (pe1 - pe0)
        return self.eval(ph, pe)


def layer_specs(params: GridJobParams, db_path: str | None = None) -> list[LayerSpec]:
    del db_path
    from ..diagram.packer import category_solid_subset, dominant_aq_species_subset, subsets_for_job

    job_phases = params.phases
    collisions = frozenset(params.solid_aqueous_collisions)
    subset_map = params.phase_names_by_subset
    specs: list[LayerSpec] = []

    for subset in subsets_for_job(params):
        key = subset_key(subset)
        eligible = frozenset(subset_map.get(key, ()))

        if params.layer_solids:
            def solid_cat(row: dict, subset: tuple[str, ...] = subset, elig: frozenset[str] = eligible) -> str:
                return category_solid_subset(
                    row, subset, eligible_phases=elig,
                    job_phases=job_phases, collision_names=collisions,
                )

            specs.append(LayerSpec(layer_id=f"solid:{key}", cat_fn=solid_cat))

        if params.layer_aqueous:
            def aq_subset_cat(row: dict, s: set[str] = set(subset)) -> str:
                return dominant_aq_species_subset(row, s)

            specs.append(LayerSpec(layer_id=f"aqueous:{key}", cat_fn=aq_subset_cat))

    return specs


def _corner_cats(
    i: int,
    j: int,
    cat_fn: Callable[[dict], str],
    base_ij: dict[tuple[int, int], Any],
) -> tuple[str, str, str, str]:
    def row_at(ii: int, jj: int) -> dict:
        r = base_ij[(ii, jj)]
        return r if isinstance(r, dict) else asdict(r)

    return (
        cat_fn(row_at(i, j)),
        cat_fn(row_at(i + 1, j)),
        cat_fn(row_at(i + 1, j + 1)),
        cat_fn(row_at(i, j + 1)),
    )


def _corner_converged(
    i: int,
    j: int,
    base_ij: dict[tuple[int, int], Any],
) -> tuple[bool, bool, bool, bool]:
    def conv(ii: int, jj: int) -> bool:
        r = base_ij[(ii, jj)]
        return r.converged if isinstance(r, GridPointResult) else bool(r.get("converged"))

    return conv(i, j), conv(i + 1, j), conv(i + 1, j + 1), conv(i, j + 1)


def _edge_coords(
    i: int,
    j: int,
    edge: int,
    base_ph: np.ndarray,
    base_pe: np.ndarray,
) -> tuple[float, float, float, float]:
    if edge == 0:
        return float(base_ph[i]), float(base_pe[j]), float(base_ph[i + 1]), float(base_pe[j])
    if edge == 1:
        return (
            float(base_ph[i + 1]),
            float(base_pe[j]),
            float(base_ph[i + 1]),
            float(base_pe[j + 1]),
        )
    if edge == 2:
        return (
            float(base_ph[i + 1]),
            float(base_pe[j + 1]),
            float(base_ph[i]),
            float(base_pe[j + 1]),
        )
    return float(base_ph[i]), float(base_pe[j + 1]), float(base_ph[i]), float(base_pe[j])


def _si_scalar(row: dict, cat_a: str, cat_b: str) -> float | None:
    si = row.get("si") or {}
    pa, pb = phase_from_label(cat_a), phase_from_label(cat_b)
    if pa not in si or pb not in si:
        return None
    va, vb = si[pa], si[pb]
    if va != va or vb != vb:
        return None
    return float(va) - float(vb)


def _species_mol(row: dict, species: str) -> float:
    mols = row.get("aq_molality_by_species") or {}
    m = mols.get(species)
    if m is None or m != m or m <= 0:
        return _MOL_FLOOR
    return float(m)


def _aq_scalar(row: dict, cat_a: str, cat_b: str) -> float | None:
    """log(m_A) - log(m_B) with floored absent species (corner-safe bracketing)."""
    if cat_a in ("none", "aqueous") or cat_b in ("none", "aqueous"):
        return None
    return math.log(_species_mol(row, cat_a)) - math.log(_species_mol(row, cat_b))


def _convergence_scalar(row: dict) -> float:
    return 1.0 if row.get("converged") else -1.0


def _single_si_scalar(row: dict, solid: str) -> float | None:
    """SI of one solid (>0 stable, <0 dissolved) -- the solid/aqueous edge."""
    si = row.get("si") or {}
    v = si.get(phase_from_label(solid))
    if v is None or v != v:
        return None
    return float(v)


def _resolve_pair_scalar(
    cat_a: str,
    cat_b: str,
    solid_phases: frozenset[str] = frozenset(),
    collisions: frozenset[str] = frozenset(),
) -> tuple[Callable[[dict], float | None] | None, str]:
    """Continuous scalar whose zero is the boundary between two categories.

    Handles solid<->solid (SI difference), aqueous<->aqueous (log-molality
    ratio), the solid<->aqueous solubility edge (single ``SI=0``; the aqueous
    side is labelled by its dominant species, not "aqueous"), and the
    converged<->failed edge bounding ``none`` regions (the stability limit,
    reused so fills stay smooth).

    Solid vs aqueous is decided structurally from the label (the ``(s)`` suffix
    on names shared with an aqueous complex), so names like ``FeO`` never need a
    saturation-index guess.
    """
    if cat_a == "none" or cat_b == "none":
        return _convergence_scalar, "conv"
    a_solid = label_is_solid(cat_a, solid_phases, collisions)
    b_solid = label_is_solid(cat_b, solid_phases, collisions)
    if a_solid and not b_solid:  # solid <-> aqueous solubility edge
        return (lambda row: _single_si_scalar(row, cat_a)), "aq_solid"
    if b_solid and not a_solid:
        return (lambda row: _single_si_scalar(row, cat_b)), "aq_solid"
    if a_solid and b_solid:  # solid <-> solid
        return (lambda row: _si_scalar(row, cat_a, cat_b)), "si"
    if cat_a == "aqueous" or cat_b == "aqueous":
        return None, ""  # generic aqueous with no solid context
    return (lambda row: _aq_scalar(row, cat_a, cat_b)), "aq"


def collect_trace_species(
    params: GridJobParams,
    base_ij: dict[tuple[int, int], Any],
    cells: list[tuple[int, int]],
    specs: list[LayerSpec],
) -> tuple[str, ...]:
    solid_set = frozenset(params.phases)
    collisions = frozenset(params.solid_aqueous_collisions)
    names: set[str] = set()
    for key in base_ij:
        r = base_ij[key]
        row = r if isinstance(r, dict) else asdict(r)
        names.update((row.get("aq_molality_by_species") or {}).keys())
        for sp in (row.get("dominant_aq_by_element") or {}).values():
            if sp and sp not in ("none", "aqueous"):
                names.add(sp)
    for spec in specs:
        for i, j in cells:
            for cat in _corner_cats(i, j, spec.cat_fn, base_ij):
                if cat in ("none", "aqueous"):
                    continue
                if label_is_solid(cat, solid_set, collisions):
                    continue
                names.add(cat)
    return tuple(sorted(names))


def _find_crossing_brentq_scalar(
    evaluator: PointEvaluator,
    scalar_fn: Callable[[dict], float],
    ph0: float,
    pe0: float,
    ph1: float,
    pe1: float,
    *,
    tol: float,
) -> float | None:
    f0 = scalar_fn(evaluator.eval(ph0, pe0))
    f1 = scalar_fn(evaluator.eval(ph1, pe1))
    if f0 * f1 > 0:
        return None

    def f(t: float) -> float:
        return scalar_fn(evaluator.eval_at_t(t, ph0, pe0, ph1, pe1))

    try:
        return float(brentq(f, 0.0, 1.0, xtol=tol, rtol=tol))
    except (ValueError, RuntimeError):
        return None


def _find_crossing_brentq(
    evaluator: PointEvaluator,
    cat_a: str,
    cat_b: str,
    ph0: float,
    pe0: float,
    ph1: float,
    pe1: float,
    *,
    tol: float,
    stats: TraceStats | None = None,
) -> float | None:
    row0 = evaluator.eval(ph0, pe0)
    row1 = evaluator.eval(ph1, pe1)
    scalar_fn, kind = _resolve_pair_scalar(
        cat_a, cat_b, evaluator.solid_phases, evaluator.collisions
    )
    if scalar_fn is None:
        return None
    f0 = scalar_fn(row0)
    f1 = scalar_fn(row1)
    if f0 is None or f1 is None or f0 * f1 > 0:
        return None

    def f(t: float) -> float:
        row = evaluator.eval_at_t(t, ph0, pe0, ph1, pe1)
        val = scalar_fn(row)
        if val is None:
            raise ValueError("boundary scalar lost bracket")
        return val

    try:
        t_cross = float(brentq(f, 0.0, 1.0, xtol=tol, rtol=tol))
        if stats:
            if kind in ("si", "aq_solid"):
                stats.n_brentq_si += 1
            elif kind == "aq":
                stats.n_brentq_aq += 1
            elif kind == "conv":
                stats.n_brentq_conv += 1
        return t_cross
    except (ValueError, RuntimeError):
        return None


def _find_convergence_crossing(
    evaluator: PointEvaluator,
    ph0: float,
    pe0: float,
    ph1: float,
    pe1: float,
    *,
    tol: float,
    stats: TraceStats | None = None,
) -> float | None:
    t = _find_crossing_brentq_scalar(
        evaluator,
        _convergence_scalar,
        ph0,
        pe0,
        ph1,
        pe1,
        tol=tol,
    )
    if t is not None and stats is not None:
        stats.n_brentq_conv += 1
    return t


def _interp_point(
    t: float, ph0: float, pe0: float, ph1: float, pe1: float
) -> tuple[float, float]:
    return ph0 + t * (ph1 - ph0), pe0 + t * (pe1 - pe0)


def count_fallback_grid_evals(factor: int) -> int:
    """New PHREEQC nodes per fallback cell (shared across all layers)."""
    if factor <= 1:
        return 0
    return (factor + 1) * (factor + 1) - 4


def _corner_base_key(
    i: int, j: int, gi: int, gj: int, factor: int
) -> tuple[int, int] | None:
    if gi == 0 and gj == 0:
        return (i, j)
    if gi == factor and gj == 0:
        return (i + 1, j)
    if gi == factor and gj == factor:
        return (i + 1, j + 1)
    if gi == 0 and gj == factor:
        return (i, j + 1)
    return None


def _fill_shared_cell_grid(
    evaluator: PointEvaluator,
    i: int,
    j: int,
    base_ph: np.ndarray,
    base_pe: np.ndarray,
    base_ij: dict[tuple[int, int], Any],
    factor: int,
) -> tuple[np.ndarray, np.ndarray, dict[tuple[int, int], dict]]:
    """Evaluate one local sub-grid per cell; corners reuse the base grid."""
    local_ph = np.linspace(float(base_ph[i]), float(base_ph[i + 1]), factor + 1)
    local_pe = np.linspace(float(base_pe[j]), float(base_pe[j + 1]), factor + 1)
    rows: dict[tuple[int, int], dict] = {}
    for gj in range(factor + 1):
        for gi in range(factor + 1):
            corner = _corner_base_key(i, j, gi, gj, factor)
            if corner is not None:
                r = base_ij[corner]
                rows[(gi, gj)] = r if isinstance(r, dict) else asdict(r)
            else:
                rows[(gi, gj)] = evaluator.eval(float(local_ph[gi]), float(local_pe[gj]))
    return local_ph, local_pe, rows


def _edge_crossing_t(
    evaluator: PointEvaluator,
    cat_a: str,
    cat_b: str,
    i: int,
    j: int,
    edge: int,
    base_ph: np.ndarray,
    base_pe: np.ndarray,
    *,
    tol: float,
    stability_tol: float,
    stats: TraceStats | None = None,
) -> float | None:
    """Parametric crossing along one edge, cached per physical edge and cat pair."""
    cached = evaluator.crossing_t_lookup(i, j, edge, cat_a, cat_b)
    if cached is not _CROSSING_UNCACHED:
        if stats is not None:
            stats.n_crossing_cache_hits += 1
        return cached  # type: ignore[return-value]
    ph0, pe0, ph1, pe1 = _edge_coords(i, j, edge, base_ph, base_pe)
    edge_tol = stability_tol if "none" in (cat_a, cat_b) else tol
    t = _find_crossing_brentq(
        evaluator, cat_a, cat_b, ph0, pe0, ph1, pe1, tol=edge_tol, stats=stats
    )
    evaluator.store_crossing_t(i, j, edge, cat_a, cat_b, t)
    return t


def _edge_local_point(edge: int, t: float, factor: int) -> tuple[float, float]:
    """Crossing point in local node coords (0..factor) for a cell edge."""
    f = float(factor)
    if edge == 0:
        return (t * f, 0.0)
    if edge == 1:
        return (f, t * f)
    if edge == 2:
        return (f - t * f, f)
    return (0.0, f - t * f)


_CORNER_LOCAL = ((0, 0), (1, 0), (1, 1), (0, 1))  # scaled by factor


def _traced_cell(
    evaluator: PointEvaluator,
    corners: tuple[str, str, str, str],
    i: int,
    j: int,
    base_ph: np.ndarray,
    base_pe: np.ndarray,
    factor: int,
    *,
    tol: float,
    stability_tol: float,
    stats: TraceStats | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]] | None:
    """Exact dividing-line geometry (+ optional boundary segment) for a 2-cat cell.

    The two edge crossings define a straight dividing line. We emit it in *local*
    fine-node coordinates (0..factor) together with the category on each side, so
    the display can build a continuous signed-distance field and recover smooth,
    sub-grid fills that coincide exactly with the boundary line. Returns ``None``
    if a bracket is lost or the cell is a saddle (caller falls back to sampling).
    """
    pts_local: list[tuple[float, float]] = []
    pts_world: list[tuple[float, float]] = []
    for edge in range(4):
        ca = corners[edge]
        cb = corners[(edge + 1) % 4]
        if ca == cb:
            continue
        t = _edge_crossing_t(
            evaluator, ca, cb, i, j, edge, base_ph, base_pe,
            tol=tol, stability_tol=stability_tol, stats=stats,
        )
        if t is None:
            return None
        pts_local.append(_edge_local_point(edge, t, factor))
        ph0, pe0, ph1, pe1 = _edge_coords(i, j, edge, base_ph, base_pe)
        pts_world.append(_interp_point(t, ph0, pe0, ph1, pe1))

    # Exactly two crossings => single dividing line. Saddles (4 crossings) and
    # degenerate cases fall back to a sampled sub-grid.
    if len(pts_local) != 2:
        return None
    p1, p2 = pts_local[0], pts_local[1]
    nx, ny = -(p2[1] - p1[1]), (p2[0] - p1[0])

    def side(px: float, py: float) -> float:
        return (px - p1[0]) * nx + (py - p1[1]) * ny

    pos_cat: str | None = None
    neg_cat: str | None = None
    for k, (cx, cy) in enumerate(_CORNER_LOCAL):
        s = side(cx * factor, cy * factor)
        if s >= 0 and pos_cat is None:
            pos_cat = corners[k]
        elif s < 0 and neg_cat is None:
            neg_cat = corners[k]
    distinct = list(dict.fromkeys(corners))
    if pos_cat is None:
        pos_cat = next((c for c in distinct if c != neg_cat), distinct[0])
    if neg_cat is None:
        neg_cat = next((c for c in distinct if c != pos_cat), distinct[0])

    line_rec = {
        "i": i,
        "j": j,
        "x1": p1[0],
        "y1": p1[1],
        "x2": p2[0],
        "y2": p2[1],
        "pos": pos_cat,
        "neg": neg_cat,
    }

    # `none` (non-stability) is treated like any other category: its edge gets a
    # normal boundary line so species/solid regions are fully outlined. The red
    # dashed stability limit is still drawn separately on top.
    segment = {
        "x": [pts_world[0][0], pts_world[1][0]],
        "y": [pts_world[0][1], pts_world[1][1]],
    }
    return segment, line_rec


def _cell_pe_ph_bounds(
    i: int, j: int, base_ph: np.ndarray, base_pe: np.ndarray
) -> tuple[float, float, float, float]:
    return (
        float(base_ph[i]),
        float(base_ph[i + 1]),
        float(base_pe[j]),
        float(base_pe[j + 1]),
    )


def _cross2d(
    ax: float, ay: float, bx: float, by: float, cx: float, cy: float
) -> float:
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def _collect_edge_crossings(
    evaluator: PointEvaluator,
    corners: tuple[str, str, str, str],
    i: int,
    j: int,
    base_ph: np.ndarray,
    base_pe: np.ndarray,
    factor: int,
    *,
    tol: float,
    stability_tol: float,
    stats: TraceStats | None = None,
) -> dict[int, tuple[tuple[float, float], tuple[float, float]]] | None:
    """Edge crossings keyed by edge index -> (local_xy, world_xy)."""
    out: dict[int, tuple[tuple[float, float], tuple[float, float]]] = {}
    for edge in range(4):
        ca = corners[edge]
        cb = corners[(edge + 1) % 4]
        if ca == cb:
            continue
        t = _edge_crossing_t(
            evaluator, ca, cb, i, j, edge, base_ph, base_pe,
            tol=tol, stability_tol=stability_tol, stats=stats,
        )
        if t is None:
            return None
        ph0, pe0, ph1, pe1 = _edge_coords(i, j, edge, base_ph, base_pe)
        out[edge] = (_edge_local_point(edge, t, factor), _interp_point(t, ph0, pe0, ph1, pe1))
    return out


def _find_interior_point_2d(
    evaluator: PointEvaluator,
    cat_a: str,
    cat_b: str,
    cat_c: str,
    ph_lo: float,
    ph_hi: float,
    pe_lo: float,
    pe_hi: float,
    x0: tuple[float, float],
    *,
    tol: float,
    stats: TraceStats | None = None,
) -> tuple[float, float] | None:
    """Locate a point where two independent pair-scalars vanish (triple point)."""
    fn_ab, _ = _resolve_pair_scalar(
        cat_a, cat_b, evaluator.solid_phases, evaluator.collisions
    )
    fn_ac, _ = _resolve_pair_scalar(
        cat_a, cat_c, evaluator.solid_phases, evaluator.collisions
    )
    if fn_ab is None or fn_ac is None:
        return None

    def residual(x: np.ndarray) -> np.ndarray:
        ph, pe = float(x[0]), float(x[1])
        row = evaluator.eval(ph, pe)
        v1 = fn_ab(row)
        v2 = fn_ac(row)
        if v1 is None or v2 is None:
            return np.array([1e3, 1e3], dtype=float)
        return np.array([v1, v2], dtype=float)

    x0_arr = np.array(x0, dtype=float)
    try:
        sol = root(residual, x0_arr, tol=tol)
        if sol.success:
            ph, pe = float(sol.x[0]), float(sol.x[1])
            if ph_lo <= ph <= ph_hi and pe_lo <= pe <= pe_hi:
                if stats is not None:
                    stats.n_brentq_2d += 1
                return ph, pe
    except (ValueError, RuntimeError):
        pass

    try:
        res = least_squares(
            residual,
            x0_arr,
            bounds=([ph_lo, pe_lo], [ph_hi, pe_hi]),
            ftol=tol,
            xtol=tol,
        )
        if res.cost < tol * 100:
            ph, pe = float(res.x[0]), float(res.x[1])
            if stats is not None:
                stats.n_brentq_2d += 1
            return ph, pe
    except (ValueError, RuntimeError):
        pass
    return None


def _world_to_local_xy(
    ph: float,
    pe: float,
    i: int,
    j: int,
    base_ph: np.ndarray,
    base_pe: np.ndarray,
    factor: int,
) -> tuple[float, float]:
    ph_lo, ph_hi, pe_lo, pe_hi = _cell_pe_ph_bounds(i, j, base_ph, base_pe)
    lx = (ph - ph_lo) / (ph_hi - ph_lo) * factor if ph_hi != ph_lo else 0.0
    ly = (pe - pe_lo) / (pe_hi - pe_lo) * factor if pe_hi != pe_lo else 0.0
    return lx, ly


def _corner_node_cat(gi: int, gj: int, corners: tuple[str, str, str, str], factor: int) -> str | None:
    if gi == 0 and gj == 0:
        return corners[0]
    if gi == factor and gj == 0:
        return corners[1]
    if gi == factor and gj == factor:
        return corners[2]
    if gi == 0 and gj == factor:
        return corners[3]
    return None


def _edge_node_cat(
    gi: int,
    gj: int,
    corners: tuple[str, str, str, str],
    crossings: dict[int, tuple[tuple[float, float], tuple[float, float]]],
    factor: int,
) -> str | None:
    """Category for a perimeter fine node using the edge brentq crossing."""
    f = float(factor)
    if gi == 0 and 0 < gj < factor:
        edge, t_node = 3, 1.0 - gj / f
    elif gi == factor and 0 < gj < factor:
        edge, t_node = 1, gj / f
    elif gj == 0 and 0 < gi < factor:
        edge, t_node = 0, gi / f
    elif gj == factor and 0 < gi < factor:
        edge, t_node = 2, 1.0 - gi / f
    else:
        return None

    ca = corners[edge]
    cb = corners[(edge + 1) % 4]
    if ca == cb:
        return ca
    hit = crossings.get(edge)
    if hit is None:
        return ca if t_node < 0.5 else cb
    lx, ly = hit[0]
    if edge == 0:
        t_cross = lx / f
    elif edge == 1:
        t_cross = ly / f
    elif edge == 2:
        t_cross = 1.0 - lx / f
    else:
        t_cross = 1.0 - ly / f
    return ca if t_node < t_cross else cb


def _ray_segments_from_point(
    T_world: tuple[float, float],
    crossings: dict[int, tuple[tuple[float, float], tuple[float, float]]],
) -> list[dict[str, Any]]:
    tx, ty = T_world
    segs: list[dict[str, Any]] = []
    for _edge, (_loc, (wx, wy)) in crossings.items():
        segs.append({"x": [tx, wx], "y": [ty, wy]})
    return segs


def _line_constraint(
    ax: float, ay: float, bx: float, by: float, tx: float, ty: float
) -> list[float]:
    """A line through (ax,ay)-(bx,by), signed so it is >= 0 at the test point.

    The stored sign lets the display rebuild a half-plane whose zero level is the
    line and whose interior (the category's side) is positive.
    """
    cross = (bx - ax) * (ty - ay) - (by - ay) * (tx - ax)
    sign = 1.0 if cross >= 0.0 else -1.0
    return [ax, ay, bx, by, sign]


def _triple_regions(
    corners: tuple[str, str, str, str],
    T_loc: tuple[float, float],
    crossings: dict[int, tuple[tuple[float, float], tuple[float, float]]],
    factor: int,
) -> list[dict[str, Any]]:
    """Convex angular sectors around a genuine triple point T (3 edge crossings).

    The 3 crossings plus a virtual ray toward the un-crossed (same-category) edge
    midpoint give 4 rays from T. Each sector between consecutive rays is the
    convex cone of the single corner it contains; the category whose two corners
    are adjacent gets two sectors (combined by union downstream). Sectors tile
    the cell, so the fills cover it with no gaps.
    """
    tx, ty = T_loc
    rays: list[tuple[float, float, int]] = []
    for edge in range(4):
        hit = crossings.get(edge)
        if hit is not None:
            px, py = hit[0]
        else:  # same-category edge: split the doubled corner span at its midpoint
            (c0x, c0y) = _CORNER_LOCAL[edge]
            (c1x, c1y) = _CORNER_LOCAL[(edge + 1) % 4]
            px, py = (c0x + c1x) * 0.5 * factor, (c0y + c1y) * 0.5 * factor
        rays.append((px, py, edge))
    rays.sort(key=lambda r: math.atan2(r[1] - ty, r[0] - tx))

    regions: list[dict[str, Any]] = []
    n = len(rays)
    for idx in range(n):
        ax, ay, ea = rays[idx]
        bx, by, eb = rays[(idx + 1) % n]
        shared = ({ea, (ea + 1) % 4} & {eb, (eb + 1) % 4})
        if not shared:
            continue
        k = shared.pop()
        cx, cy = _CORNER_LOCAL[k]
        regions.append(
            {
                "cat": corners[k],
                "lines": [
                    _line_constraint(tx, ty, ax, ay, cx * factor, cy * factor),
                    _line_constraint(tx, ty, bx, by, cx * factor, cy * factor),
                ],
            }
        )
    return regions


def _band_regions(
    corners: tuple[str, str, str, str],
    crossings: dict[int, tuple[tuple[float, float], tuple[float, float]]],
    factor: int,
) -> list[dict[str, Any]]:
    """3-category cell with 4 crossings: a band of the doubled (diagonal) category.

    The two single-corner categories are each cut off by the line joining the
    crossings on their adjacent edges; the doubled category is the convex strip
    between those two cuts (intersection / min of both, oriented toward it).
    """
    counts = Counter(corners)
    doubled = next((c for c, n in counts.items() if n == 2), None)
    if doubled is None:
        return []
    d_corner = next(k for k in range(4) if corners[k] == doubled)
    dcx, dcy = _CORNER_LOCAL[d_corner]
    cat_lines: dict[str, list[list[float]]] = defaultdict(list)
    for k in range(4):
        if corners[k] == doubled:
            continue
        e_prev, e_cur = (k - 1) % 4, k
        if e_prev not in crossings or e_cur not in crossings:
            return []
        (px, py) = crossings[e_prev][0]
        (qx, qy) = crossings[e_cur][0]
        cx, cy = _CORNER_LOCAL[k]
        cat_lines[corners[k]].append(
            _line_constraint(px, py, qx, qy, cx * factor, cy * factor)
        )
        cat_lines[doubled].append(
            _line_constraint(px, py, qx, qy, dcx * factor, dcy * factor)
        )
    return [{"cat": cat, "lines": lines} for cat, lines in cat_lines.items()]


def _band_segments(
    corners: tuple[str, str, str, str],
    crossings: dict[int, tuple[tuple[float, float], tuple[float, float]]],
) -> list[dict[str, Any]]:
    """World-coordinate boundary segments for a band cell (the two corner cuts)."""
    counts = Counter(corners)
    doubled = next((c for c, n in counts.items() if n == 2), None)
    segs: list[dict[str, Any]] = []
    for k in range(4):
        if corners[k] == doubled:
            continue
        e_prev, e_cur = (k - 1) % 4, k
        if e_prev not in crossings or e_cur not in crossings:
            continue
        (_lp, (wx0, wy0)) = crossings[e_prev]
        (_lq, (wx1, wy1)) = crossings[e_cur]
        segs.append({"x": [wx0, wx1], "y": [wy0, wy1]})
    return segs


def _regions_node_cats(
    regions: list[dict[str, Any]], factor: int
) -> dict[tuple[int, int], str]:
    """Integer node labels: each node takes the region whose min-field is largest."""
    out: dict[tuple[int, int], str] = {}
    for gj in range(factor + 1):
        for gi in range(factor + 1):
            best_cat = regions[0]["cat"]
            best_val = -math.inf
            for region in regions:
                val = math.inf
                for ax, ay, bx, by, sign in region["lines"]:
                    cross = (bx - ax) * (gj - ay) - (by - ay) * (gi - ax)
                    val = min(val, sign * cross)
                if val > best_val:
                    best_val = val
                    best_cat = region["cat"]
            out[(gi, gj)] = best_cat
    return out


def _trace_triple_cell(
    evaluator: PointEvaluator,
    corners: tuple[str, str, str, str],
    i: int,
    j: int,
    base_ph: np.ndarray,
    base_pe: np.ndarray,
    factor: int,
    *,
    tol: float,
    stability_tol: float,
    stats: TraceStats | None = None,
) -> tuple[dict[tuple[int, int], str], list[dict[str, Any]], dict[str, Any] | None] | None:
    """3-category cell: edge brentq plus convex per-category fill regions.

    Three crossings => a genuine triple point (located by a 2D scalar root, or
    the crossing centroid when a category is ``none`` and one scalar is the
    convergence step). Four crossings => the doubled category forms a band, which
    needs no interior point. Both yield convex line-bounded regions so fills are
    smooth and coincide with the boundary segments.
    """
    distinct = list(dict.fromkeys(corners))
    if len(distinct) != 3:
        return None
    crossings = _collect_edge_crossings(
        evaluator, corners, i, j, base_ph, base_pe, factor,
        tol=tol, stability_tol=stability_tol, stats=stats,
    )
    if not crossings:
        return None

    if len(crossings) == 4:
        regions = _band_regions(corners, crossings, factor)
        if not regions:
            return None
        node_cats = _regions_node_cats(regions, factor)
        segments = _band_segments(corners, crossings)
        return node_cats, segments, {"i": i, "j": j, "regions": regions}

    if len(crossings) != 3:
        return None

    ph_lo, ph_hi, pe_lo, pe_hi = _cell_pe_ph_bounds(i, j, base_ph, base_pe)
    x0 = (
        sum(w[0] for _, w in crossings.values()) / len(crossings),
        sum(w[1] for _, w in crossings.values()) / len(crossings),
    )
    T_world: tuple[float, float] | None = None
    # Try each category as the reference for the 2x2 scalar system.
    for ref, other_a, other_b in (
        (distinct[0], distinct[1], distinct[2]),
        (distinct[1], distinct[0], distinct[2]),
        (distinct[2], distinct[0], distinct[1]),
    ):
        T_world = _find_interior_point_2d(
            evaluator,
            ref,
            other_a,
            other_b,
            ph_lo,
            ph_hi,
            pe_lo,
            pe_hi,
            x0,
            tol=tol,
            stats=stats,
        )
        if T_world is not None:
            break
    # Crossing centroid: an always-interior junction estimate. Used when the 2D
    # solve cannot converge (a category is ``none``, so one pair-scalar is the
    # convergence step) and as a guard when the solver clamps T onto a cell edge
    # (degenerate: it collapses a sector and leaves a fill gap).
    T_loc = (
        None
        if T_world is None
        else _world_to_local_xy(T_world[0], T_world[1], i, j, base_ph, base_pe, factor)
    )
    margin = 0.05 * factor
    if T_loc is None or not (
        margin <= T_loc[0] <= factor - margin and margin <= T_loc[1] <= factor - margin
    ):
        T_world = x0
        T_loc = _world_to_local_xy(
            T_world[0], T_world[1], i, j, base_ph, base_pe, factor
        )
    regions = _triple_regions(corners, T_loc, crossings, factor)
    node_cats = _regions_node_cats(regions, factor)
    segments = _ray_segments_from_point(T_world, crossings)
    return node_cats, segments, {"i": i, "j": j, "regions": regions}


def _saddle_node_cats(
    corners: tuple[str, str, str, str],
    p02_a: tuple[float, float],
    p02_b: tuple[float, float],
    p13_a: tuple[float, float],
    p13_b: tuple[float, float],
    factor: int,
    crossings: dict[int, tuple[tuple[float, float], tuple[float, float]]],
) -> dict[tuple[int, int], str]:
    """2-category saddle (4 crossings): split by two exact crossing lines."""
    corner_xy = [(cx * factor, cy * factor) for cx, cy in _CORNER_LOCAL]

    def signs(px: float, py: float) -> tuple[bool, bool]:
        s1 = _cross2d(p02_a[0], p02_a[1], p02_b[0], p02_b[1], px, py) >= 0.0
        s2 = _cross2d(p13_a[0], p13_a[1], p13_b[0], p13_b[1], px, py) >= 0.0
        return s1, s2

    corner_signs = [signs(px, py) for px, py in corner_xy]
    out: dict[tuple[int, int], str] = {}
    for gj in range(factor + 1):
        for gi in range(factor + 1):
            corner = _corner_node_cat(gi, gj, corners, factor)
            if corner is not None:
                out[(gi, gj)] = corner
                continue
            edge = _edge_node_cat(gi, gj, corners, crossings, factor)
            if edge is not None:
                out[(gi, gj)] = edge
                continue
            s = signs(float(gi), float(gj))
            for k, cs in enumerate(corner_signs):
                if cs == s:
                    out[(gi, gj)] = corners[k]
                    break
            else:
                out[(gi, gj)] = corners[0]
    return out


def _trace_saddle_cell(
    evaluator: PointEvaluator,
    corners: tuple[str, str, str, str],
    i: int,
    j: int,
    base_ph: np.ndarray,
    base_pe: np.ndarray,
    factor: int,
    *,
    tol: float,
    stability_tol: float,
    stats: TraceStats | None = None,
) -> tuple[dict[tuple[int, int], str], list[dict[str, Any]]] | None:
    """2-category cell with four edge crossings (saddle topology)."""
    if len(set(corners)) != 2:
        return None
    crossings = _collect_edge_crossings(
        evaluator, corners, i, j, base_ph, base_pe, factor,
        tol=tol, stability_tol=stability_tol, stats=stats,
    )
    if crossings is None or len(crossings) != 4:
        return None

    p0 = crossings[0][0]
    p1 = crossings[1][0]
    p2 = crossings[2][0]
    p3 = crossings[3][0]
    node_cats = _saddle_node_cats(corners, p0, p2, p1, p3, factor, crossings)
    segments = [
        {
            "x": [crossings[0][1][0], crossings[2][1][0]],
            "y": [crossings[0][1][1], crossings[2][1][1]],
        },
        {
            "x": [crossings[1][1][0], crossings[3][1][0]],
            "y": [crossings[1][1][1], crossings[3][1][1]],
        },
    ]
    return node_cats, segments


def _classify_cell(
    evaluator: PointEvaluator,
    spec: LayerSpec,
    i: int,
    j: int,
    base_ph: np.ndarray,
    base_pe: np.ndarray,
    base_ij: dict[tuple[int, int], Any],
    factor: int,
    *,
    tol: float,
    stability_tol: float,
    stats: TraceStats | None = None,
) -> tuple[
    dict[str, Any] | None,
    dict[tuple[int, int], str] | None,
    list[dict[str, Any]],
    dict[str, Any] | None,
    bool,
]:
    """Classify one cell+layer for tracing.

    Returns ``(line_rec, node_cats, segments, region_rec, need_fallback)``.
    Clean 2-category cells use ``line_rec``; triple/band/saddle cells use
    ``node_cats`` plus boundary ``segments`` (and, for 3-category cells,
    ``region_rec`` with convex per-category fill geometry).
    """
    cat_fn = spec.cat_fn
    corners = _corner_cats(i, j, cat_fn, base_ij)
    distinct = set(corners)
    if len(distinct) <= 1:
        return None, None, [], None, False

    if len(distinct) == 3:
        triple = _trace_triple_cell(
            evaluator, corners, i, j, base_ph, base_pe, factor,
            tol=tol, stability_tol=stability_tol, stats=stats,
        )
        if triple is not None:
            node_cats, segments, region_rec = triple
            if stats is not None:
                stats.n_cells_triple_traced += 1
            return None, node_cats, segments, region_rec, False

    if len(distinct) == 2:
        saddle = _trace_saddle_cell(
            evaluator, corners, i, j, base_ph, base_pe, factor,
            tol=tol, stability_tol=stability_tol, stats=stats,
        )
        if saddle is not None:
            node_cats, segments = saddle
            if stats is not None:
                stats.n_cells_saddle_traced += 1
            return None, node_cats, segments, None, False

    if len(distinct) > 2:
        if stats is not None:
            stats.n_cells_complex_fallback += 1
        return None, None, [], None, True

    traced = _traced_cell(
        evaluator, corners, i, j, base_ph, base_pe, factor,
        tol=tol, stability_tol=stability_tol, stats=stats,
    )
    if traced is None:
        return None, None, [], None, True
    segment, line_rec = traced
    segments = [segment] if segment is not None else []
    return line_rec, None, segments, None, False


def _local_mask_contours(
    mask: np.ndarray,
    local_ph: np.ndarray,
    local_pe: np.ndarray,
) -> list[tuple[np.ndarray, np.ndarray]]:
    n_pe, n_ph = mask.shape
    # Pad with edge values so a category that reaches the cell border does NOT
    # spawn a spurious contour ring along the border (the "box" artifact);
    # only genuine interior category interfaces produce lines.
    padded = np.pad(mask.astype(float), 1, mode="edge")
    rings = find_contours(padded, 0.5)
    idx_ph = np.arange(n_ph)
    idx_pe = np.arange(n_pe)
    out: list[tuple[np.ndarray, np.ndarray]] = []
    for ring in rings:
        rows = np.clip(ring[:, 0] - 1, 0, n_pe - 1)
        cols = np.clip(ring[:, 1] - 1, 0, n_ph - 1)
        xs = np.interp(cols, idx_ph, local_ph)
        ys = np.interp(rows, idx_pe, local_pe)
        out.append((xs, ys))
    return out


def _fallback_cell_boundaries(
    local_ph: np.ndarray,
    local_pe: np.ndarray,
    grid_rows: dict[tuple[int, int], dict],
    cat_fn: Callable[[dict], str],
    factor: int,
) -> list[dict[str, Any]]:
    """Boundary polylines from a sampled fallback sub-grid (triple points etc.)."""
    n = factor + 1
    name_by_node = {(gi, gj): cat_fn(grid_rows[(gi, gj)]) for gj in range(n) for gi in range(n)}
    cats = set(name_by_node.values())
    if len(cats) <= 1:
        return []
    names = sorted(cats)
    name_index = {name: k for k, name in enumerate(names)}
    grid = np.empty((n, n), dtype=int)
    for (gi, gj), name in name_by_node.items():
        grid[gj, gi] = name_index[name]

    segments: list[dict[str, Any]] = []
    for name in names:
        if name in ("none", "aqueous"):
            continue
        mask = grid == name_index[name]
        if not mask.any():
            continue
        for xs, ys in _local_mask_contours(mask, local_ph, local_pe):
            segments.append({"x": [float(v) for v in xs], "y": [float(v) for v in ys]})
    return segments


def _trace_stability_cell(
    evaluator: PointEvaluator,
    i: int,
    j: int,
    base_ph: np.ndarray,
    base_pe: np.ndarray,
    base_ij: dict[tuple[int, int], Any],
    *,
    stability_tol: float,
    stats: TraceStats | None = None,
) -> list[dict[str, Any]]:
    """Trace converged / failed transition edges (stability limit)."""
    conv = _corner_converged(i, j, base_ij)
    if all(conv) or not any(conv):
        return []

    segments: list[dict[str, Any]] = []
    for edge in range(4):
        c0 = conv[edge]
        c1 = conv[(edge + 1) % 4]
        if c0 == c1:
            continue
        ph0, pe0, ph1, pe1 = _edge_coords(i, j, edge, base_ph, base_pe)
        cached = evaluator.convergence_crossing_t_lookup(i, j, edge)
        if cached is not _CROSSING_UNCACHED:
            if stats is not None:
                stats.n_crossing_cache_hits += 1
            t = cached  # type: ignore[assignment]
        else:
            t = _find_convergence_crossing(
                evaluator, ph0, pe0, ph1, pe1, tol=stability_tol, stats=stats
            )
            evaluator.store_convergence_crossing_t(i, j, edge, t)
        if t is None:
            continue
        x, y = _interp_point(t, ph0, pe0, ph1, pe1)
        segments.append(
            {
                "kind": "stability_limit",
                "between": ["converged", "failed"],
                "x": [x],
                "y": [y],
            }
        )
    return segments


def _corner_seed(
    cells: list[tuple[int, int]],
    base_ij: dict[tuple[int, int], Any],
) -> dict[tuple[int, int], dict]:
    needed: set[tuple[int, int]] = set()
    for i, j in cells:
        for di, dj in ((0, 0), (1, 0), (1, 1), (0, 1)):
            needed.add((i + di, j + dj))
    out: dict[tuple[int, int], dict] = {}
    for key in needed:
        r = base_ij[key]
        out[key] = r if isinstance(r, dict) else asdict(r)
    return out


def _trace_cells_batch(
    cells: list[tuple[int, int]],
    trace_params: GridJobParams,
    db_path: str,
    base_ph: np.ndarray,
    base_pe: np.ndarray,
    corner_ij: dict[tuple[int, int], dict],
    *,
    tol: float,
    stability_tol: float,
    factor: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], TraceStats]:
    """Trace all layers + stability limits for a batch of cells (one worker)."""
    seed: dict[tuple[float, float], dict] = {}
    for row in corner_ij.values():
        seed[_point_key(float(row["ph"]), float(row["pe"]))] = row

    evaluator = PointEvaluator(trace_params, seed)
    specs = layer_specs(trace_params, db_path)
    stats = TraceStats()
    # Per layer: clean-cell dividing lines (smooth fills), sampled fallback nodes,
    # and exact boundary segments (lines).
    cell_lines: dict[str, list[dict[str, Any]]] = {spec.layer_id: [] for spec in specs}
    cell_regions: dict[str, list[dict[str, Any]]] = {spec.layer_id: [] for spec in specs}
    node_overrides: dict[str, dict[tuple[int, int], str]] = {
        spec.layer_id: {} for spec in specs
    }
    boundaries: dict[str, list[dict[str, Any]]] = {spec.layer_id: [] for spec in specs}
    stability: list[dict[str, Any]] = []
    fallback_by_cell: dict[tuple[int, int], list[LayerSpec]] = defaultdict(list)

    def add_nodes(layer_id: str, i: int, j: int, cats: dict[tuple[int, int], str]) -> None:
        dest = node_overrides[layer_id]
        for (gi, gj), name in cats.items():
            dest[(i * factor + gi, j * factor + gj)] = name

    for spec in specs:
        for i, j in cells:
            line_rec, node_cats, segments, region_rec, need_fallback = _classify_cell(
                evaluator,
                spec,
                i,
                j,
                base_ph,
                base_pe,
                corner_ij,
                factor,
                tol=tol,
                stability_tol=stability_tol,
                stats=stats,
            )
            if need_fallback:
                fallback_by_cell[(i, j)].append(spec)
            elif node_cats is not None:
                add_nodes(spec.layer_id, i, j, node_cats)
                boundaries[spec.layer_id].extend(segments)
                if region_rec is not None:
                    cell_regions[spec.layer_id].append(region_rec)
                stats.n_cells_traced += 1
            elif line_rec is not None:
                cell_lines[spec.layer_id].append(line_rec)
                boundaries[spec.layer_id].extend(segments)
                stats.n_cells_traced += 1

    for (i, j), fb_specs in fallback_by_cell.items():
        stats.n_cells_fallback += 1
        fb_before = evaluator.n_evals
        local_ph, local_pe, grid_rows = _fill_shared_cell_grid(
            evaluator, i, j, base_ph, base_pe, corner_ij, factor
        )
        stats.n_fallback_evals += evaluator.n_evals - fb_before
        for spec in fb_specs:
            cats = {
                (gi, gj): spec.cat_fn(grid_rows[(gi, gj)])
                for gj in range(factor + 1)
                for gi in range(factor + 1)
            }
            add_nodes(spec.layer_id, i, j, cats)
            boundaries[spec.layer_id].extend(
                _fallback_cell_boundaries(
                    local_ph, local_pe, grid_rows, spec.cat_fn, factor
                )
            )

    for i, j in cells:
        stability.extend(
            _trace_stability_cell(
                evaluator, i, j, base_ph, base_pe, corner_ij,
                stability_tol=stability_tol, stats=stats,
            )
        )

    # Serialize node overrides as parallel lists for IPC.
    layers_out: dict[str, Any] = {}
    for layer_id, nodes in node_overrides.items():
        fis: list[int] = []
        fjs: list[int] = []
        cats: list[str] = []
        for (fi, fj), name in nodes.items():
            fis.append(fi)
            fjs.append(fj)
            cats.append(name)
        layers_out[layer_id] = {
            "node_fi": fis,
            "node_fj": fjs,
            "node_cat": cats,
            "cell_lines": cell_lines[layer_id],
            "cell_regions": cell_regions[layer_id],
            "boundaries": boundaries[layer_id],
        }

    stats.n_stability_segments = len(stability)
    stats.n_trace_evals = evaluator.n_evals - stats.n_fallback_evals
    return layers_out, stability, stats


_WORKER_PQ = None
_WORKER_TRACE_PARAMS: GridJobParams | None = None
_WORKER_BASE_PH: np.ndarray | None = None
_WORKER_BASE_PE: np.ndarray | None = None
_WORKER_TOL: float = 0.0
_WORKER_STABILITY_TOL: float = 0.0
_WORKER_FACTOR: int = 1


def _trace_worker_init(
    dll_path: str,
    db_path: str,
    trace_params_dict: dict[str, Any],
    base_ph: list[float],
    base_pe: list[float],
    tol: float,
    stability_tol: float,
    factor: int,
) -> None:
    global _WORKER_PQ, _WORKER_TRACE_PARAMS, _WORKER_BASE_PH, _WORKER_BASE_PE
    global _WORKER_TOL, _WORKER_STABILITY_TOL, _WORKER_FACTOR
    _WORKER_PQ = init_phreeqc(dll_path, db_path)
    _WORKER_TRACE_PARAMS = grid_job_params_from_dict(trace_params_dict)
    _WORKER_BASE_PH = np.asarray(base_ph, dtype=float)
    _WORKER_BASE_PE = np.asarray(base_pe, dtype=float)
    _WORKER_TOL = float(tol)
    _WORKER_STABILITY_TOL = float(stability_tol)
    _WORKER_FACTOR = int(factor)


def _trace_worker_job(job: dict[str, Any]) -> dict[str, Any]:
    """Process-pool entry: trace a chunk of boundary cells."""
    cells: list[tuple[int, int]] = job["cells"]
    corner_ij = {
        tuple(int(p) for p in k.split(",")): v for k, v in job["corner_ij"].items()
    }
    assert _WORKER_TRACE_PARAMS is not None
    assert _WORKER_BASE_PH is not None
    assert _WORKER_BASE_PE is not None

    layers_out, stability, stats = _trace_cells_batch(
        cells,
        _WORKER_TRACE_PARAMS,
        _WORKER_TRACE_PARAMS.db_path,
        _WORKER_BASE_PH,
        _WORKER_BASE_PE,
        corner_ij,
        tol=_WORKER_TOL,
        stability_tol=_WORKER_STABILITY_TOL,
        factor=_WORKER_FACTOR,
    )
    return {
        "layers": layers_out,
        "stability": stability,
        "stats": asdict(stats),
    }


def _morton_code(i: int, j: int) -> int:
    """2D Morton (Z-order) key for grid cell lower-left index ``(i, j)``."""
    code = 0
    for bit in range(16):
        code |= ((i >> bit) & 1) << (2 * bit + 1)
        code |= ((j >> bit) & 1) << (2 * bit)
    return code


def _sort_cells_morton(cells: list[tuple[int, int]]) -> list[tuple[int, int]]:
    return sorted(cells, key=lambda ij: _morton_code(ij[0], ij[1]))


def _chunk_cells(
    cells: list[tuple[int, int]],
    *,
    workers: int,
) -> list[list[tuple[int, int]]]:
    """Split mixed cells into worker chunks (Morton order, contiguous blocks)."""
    if not cells:
        return []
    mult = config.TRACE_CHUNK_MULTIPLIER
    min_chunk = config.TRACE_MIN_CELLS_PER_CHUNK
    if workers <= 1 or len(cells) <= min_chunk:
        return [cells]

    ordered = _sort_cells_morton(cells)

    target_chunks = workers * max(1, mult)
    max_chunks = max(1, len(ordered) // min_chunk)
    n_chunks = min(target_chunks, max_chunks, len(ordered))
    n_chunks = max(n_chunks, workers)

    base, extra = divmod(len(ordered), n_chunks)
    chunks: list[list[tuple[int, int]]] = []
    start = 0
    for ci in range(n_chunks):
        size = base + (1 if ci < extra else 0)
        if size:
            chunks.append(ordered[start : start + size])
            start += size
    return chunks


def run_boundary_trace(
    params: GridJobParams,
    *,
    db_path: str,
    base_ph: np.ndarray,
    base_pe: np.ndarray,
    base_ij: dict[tuple[int, int], GridPointResult],
    cells: list[tuple[int, int]],
    tolerance: float | None = None,
    stability_tolerance: float | None = None,
    refine_factor: int | None = None,
    progress_cb: Callable[[int, int], None] | None = None,
    max_workers: int | None = None,
) -> tuple[dict[str, Any], TraceStats]:
    """Trace boundaries across all plottable layers for mixed base cells."""
    tol = tolerance or config.BOUNDARY_TRACE_TOLERANCE
    stability_tol = stability_tolerance or config.BOUNDARY_TRACE_STABILITY_TOLERANCE
    factor = refine_factor or config.ADAPTIVE_REFINE_FACTOR
    specs = layer_specs(params, db_path)
    species = collect_trace_species(params, base_ij, cells, specs)
    trace_params = replace(
        params,
        aq_species_molality=species,
        top_aq_species_per_element=config.BOUNDARY_TRACE_TOP_AQ_SPECIES,
    )

    workers = max_workers if max_workers is not None else config.MAX_WORKERS
    chunks = _chunk_cells(cells, workers=workers)

    if workers <= 1 or len(chunks) <= 1:
        corner_ij = _corner_seed(cells, base_ij)
        layers_out, stability, stats = _trace_cells_batch(
            cells,
            trace_params,
            db_path,
            base_ph,
            base_pe,
            corner_ij,
            tol=tol,
            stability_tol=stability_tol,
            factor=factor,
        )
        if progress_cb:
            progress_cb(1, 1)
    else:
        jobs = []
        for chunk in chunks:
            corner_ij = _corner_seed(chunk, base_ij)
            serial_corner = {f"{i},{j}": v for (i, j), v in corner_ij.items()}
            jobs.append({"cells": chunk, "corner_ij": serial_corner})

        layers_out = {}
        stability: list[dict[str, Any]] = []
        stats = TraceStats()
        done = 0
        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=_trace_worker_init,
            initargs=(
                params.dll_path,
                params.db_path,
                asdict(trace_params),
                base_ph.tolist(),
                base_pe.tolist(),
                tol,
                stability_tol,
                factor,
            ),
        ) as pool:
            for result in pool.map(_trace_worker_job, jobs):
                chunk_stats = TraceStats(**result["stats"])
                _merge_stats(stats, chunk_stats)
                for layer_id, data in result["layers"].items():
                    if layer_id not in layers_out:
                        layers_out[layer_id] = {
                            "node_fi": [],
                            "node_fj": [],
                            "node_cat": [],
                            "cell_lines": [],
                            "cell_regions": [],
                            "boundaries": [],
                        }
                    layers_out[layer_id]["node_fi"].extend(data["node_fi"])
                    layers_out[layer_id]["node_fj"].extend(data["node_fj"])
                    layers_out[layer_id]["node_cat"].extend(data["node_cat"])
                    layers_out[layer_id]["cell_lines"].extend(
                        data.get("cell_lines", [])
                    )
                    layers_out[layer_id]["cell_regions"].extend(
                        data.get("cell_regions", data.get("cell_wedges", []))
                    )
                    layers_out[layer_id]["boundaries"].extend(
                        data.get("boundaries", [])
                    )
                stability.extend(result["stability"])
                done += 1
                if progress_cb:
                    progress_cb(done, len(chunks))

        stats.n_stability_segments = len(stability)

    stats.n_segments = sum(len(layer["node_cat"]) for layer in layers_out.values())

    from .gas_limits import trace_gas_limit_segments

    gas_segments = trace_gas_limit_segments(
        trace_params,
        base_ph=base_ph,
        base_pe=base_pe,
        base_ij=base_ij,
        evaluator=PointEvaluator(trace_params, {
            _point_key(float(r.ph), float(r.pe)): asdict(r) for r in base_ij.values()
        }),
        tolerance=tol,
    )
    stats.n_gas_segments = len(gas_segments)

    trace_bundle: dict[str, Any] = {
        "method": "traced",
        "tolerance": tol,
        "stability_tolerance": stability_tol,
        "refine_factor": factor,
        "top_aq_species_per_element": trace_params.top_aq_species_per_element,
        "aq_species": list(species),
        "stability_limits": {
            "kind": "stability_limit",
            "segments": stability,
        },
        "gas_limits": {
            "kind": "gas_limit",
            "segments": gas_segments,
        },
        "layers": layers_out,
        "stats": {
            "n_trace_evals": stats.n_trace_evals,
            "n_fallback_evals": stats.n_fallback_evals,
            "n_crossings": stats.n_crossings,
            "n_cells_traced": stats.n_cells_traced,
            "n_cells_fallback": stats.n_cells_fallback,
            "n_segments": stats.n_segments,
            "n_brentq_si": stats.n_brentq_si,
            "n_brentq_aq": stats.n_brentq_aq,
            "n_brentq_conv": stats.n_brentq_conv,
            "n_stability_segments": stats.n_stability_segments,
            "n_gas_segments": stats.n_gas_segments,
            "n_cells_complex_fallback": stats.n_cells_complex_fallback,
            "n_crossing_cache_hits": stats.n_crossing_cache_hits,
            "n_cells_triple_traced": stats.n_cells_triple_traced,
            "n_cells_saddle_traced": stats.n_cells_saddle_traced,
            "n_brentq_2d": stats.n_brentq_2d,
        },
    }
    return trace_bundle, stats
