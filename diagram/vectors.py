"""Vector display for traced adaptive boundaries.

Each display layer is built from the trace bundle:

1. A fine categorical grid (base block-fill + traced overrides for boundary cells).
2. Per-category signed-distance fields from exact dividing lines (2-category cells)
   and convex line-bounded regions (3-category triple/band cells).
3. Zero-level contouring of those fields for smooth fills aligned with the exact
   boundary segments emitted by the tracer.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable

import numpy as np
from skimage.measure import find_contours

# Sentinels for the per-category signed-distance fill field.
_FIELD_BIG = 1.0e4  # interior magnitude; far exceeds any local signed distance
_FIELD_PAD = -1.0e6  # border pad: closes regions touching the plot frame

from ..phreeqc.adaptive import fine_axis_levels
from ..phreeqc.engine import GridJobParams
from ..phreeqc.gas_limits import (
    water_gas_boundary_segments,
    water_gas_outside_labels,
    water_gas_scalar_grids,
    water_gas_sum_window,
)
from .packer import (
    category_solid_subset,
    count_layer_pack_steps,
    dominant_aq_species_subset,
    label_is_solid,
    subset_key,
    subsets_for_job,
)


def _base_index_grid(
    base_rows: list[dict],
    *,
    n_ph: int,
    n_pe: int,
    ph_lookup: dict[float, int],
    pe_lookup: dict[float, int],
    cat_fn: Callable[[dict], str],
    name_index: dict[str, int],
) -> np.ndarray:
    grid = np.full((n_pe, n_ph), -1, dtype=int)
    for row in base_rows:
        ix = ph_lookup.get(round(float(row["ph"]), 12))
        iy = pe_lookup.get(round(float(row["pe"]), 12))
        if ix is None or iy is None:
            continue
        grid[iy, ix] = name_index.get(cat_fn(row), -1)
    return grid


def _fine_index_grid(
    base_idx: np.ndarray,
    *,
    factor: int,
    n_ph_fine: int,
    n_pe_fine: int,
    overrides: dict[tuple[int, int], int],
) -> np.ndarray:
    n_pe, n_ph = base_idx.shape
    fi_to_bi = np.clip(np.round(np.arange(n_ph_fine) / factor).astype(int), 0, n_ph - 1)
    fj_to_bj = np.clip(np.round(np.arange(n_pe_fine) / factor).astype(int), 0, n_pe - 1)
    fine = base_idx[np.ix_(fj_to_bj, fi_to_bi)].copy()
    for (fi, fj), cat in overrides.items():
        if 0 <= fj < n_pe_fine and 0 <= fi < n_ph_fine:
            fine[fj, fi] = cat
    return fine


def _field_contours(
    field: np.ndarray,
    fine_ph: np.ndarray,
    fine_pe: np.ndarray,
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Zero-level contours of a signed field (>0 inside the category region).

    Padding with a strongly negative constant closes any region that reaches the
    plot frame, mirroring binary-mask behaviour while letting the interior carry
    a continuous signed distance so the contour lands exactly on the boundary.
    """
    n_pe, n_ph = field.shape
    padded = np.pad(field, 1, mode="constant", constant_values=_FIELD_PAD)
    rings = find_contours(padded, 0.0)
    idx_ph = np.arange(n_ph)
    idx_pe = np.arange(n_pe)
    out: list[tuple[np.ndarray, np.ndarray]] = []
    for ring in rings:
        rows = np.clip(ring[:, 0] - 1, 0, n_pe - 1)
        cols = np.clip(ring[:, 1] - 1, 0, n_ph - 1)
        xs = np.interp(cols, idx_ph, fine_ph)
        ys = np.interp(rows, idx_pe, fine_pe)
        out.append((xs, ys))
    return out


def _despeckle(fine: np.ndarray) -> np.ndarray:
    """Remove single-node speckle: a node whose 8 neighbours unanimously agree
    on a different category is flipped to that category. Smooth traced regions
    have no such isolated nodes, so only sampled-fallback noise is cleaned."""
    if fine.shape[0] < 3 or fine.shape[1] < 3:
        return fine
    neigh = [
        np.roll(np.roll(fine, dy, 0), dx, 1)
        for dy in (-1, 0, 1)
        for dx in (-1, 0, 1)
        if not (dx == 0 and dy == 0)
    ]
    stack = np.stack(neigh)
    first = stack[0]
    unanimous = np.all(stack == first, axis=0)
    isolated = unanimous & (fine != first)
    # Don't touch the border ring (np.roll wraps it).
    isolated[0, :] = isolated[-1, :] = isolated[:, 0] = isolated[:, -1] = False
    if not isolated.any():
        return fine
    out = fine.copy()
    out[isolated] = first[isolated]
    return out


def _parse_cell_lines(
    cell_lines: list[dict[str, Any]], name_index: dict[str, int]
) -> list[dict[str, Any]]:
    """Precompute integer categories and line normals for clean boundary cells."""
    recs: list[dict[str, Any]] = []
    for r in cell_lines:
        x1, y1, x2, y2 = (
            float(r["x1"]),
            float(r["y1"]),
            float(r["x2"]),
            float(r["y2"]),
        )
        recs.append(
            {
                "i": int(r["i"]),
                "j": int(r["j"]),
                "x1": x1,
                "y1": y1,
                "nx": -(y2 - y1),
                "ny": (x2 - x1),
                "pos": name_index.get(r["pos"], -1),
                "neg": name_index.get(r["neg"], -1),
            }
        )
    return recs


def _apply_cell_lines_to_grid(
    fine: np.ndarray,
    recs: list[dict[str, Any]],
    *,
    factor: int,
    grid_li: np.ndarray,
    grid_lj: np.ndarray,
) -> None:
    """Label each clean-cell fine node by which side of its dividing line it lies."""
    n_pe_fine, n_ph_fine = fine.shape
    for r in recs:
        s = (grid_li - r["x1"]) * r["nx"] + (grid_lj - r["y1"]) * r["ny"]
        block = np.where(s >= 0.0, r["pos"], r["neg"])
        fi0, fj0 = r["i"] * factor, r["j"] * factor
        fi1, fj1 = min(fi0 + factor + 1, n_ph_fine), min(fj0 + factor + 1, n_pe_fine)
        fine[fj0:fj1, fi0:fi1] = block[: fj1 - fj0, : fi1 - fi0]


def _parse_cell_regions(
    cell_regions: list[dict[str, Any]], name_index: dict[str, int]
) -> dict[int, list[dict[str, Any]]]:
    """Group per-cell fill regions by the category index they cover.

    Each region is a convex set defined by oriented lines
    ``[ax, ay, bx, by, sign]`` in local fine-node coordinates; the category fills
    where every signed distance is >= 0. Triple cells give one or more angular
    sectors per category; band cells give a half-plane per single corner and a
    two-line strip for the doubled category.
    """
    by_cat: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for cell in cell_regions:
        i, j = int(cell["i"]), int(cell["j"])
        for region in cell.get("regions", ()):
            cat = name_index.get(region["cat"], -1)
            if cat < 0:
                continue
            lines = [
                (float(a), float(b), float(c), float(d), float(s))
                for a, b, c, d, s in region["lines"]
            ]
            if not lines:
                continue
            by_cat[cat].append({"i": i, "j": j, "lines": lines})
    return dict(by_cat)


def _region_field_block(
    region: dict[str, Any], grid_li: np.ndarray, grid_lj: np.ndarray
) -> np.ndarray:
    """Signed field for one convex region: min of its oriented line half-planes.

    Each line contributes ``sign * cross(A, B, node)`` (positive on the region's
    side). The per-node minimum is >0 strictly inside and 0 on the bounding lines,
    so contouring at 0 reproduces the exact edges.
    """
    field: np.ndarray | None = None
    for ax, ay, bx, by, sign in region["lines"]:
        cross = (bx - ax) * (grid_lj - ay) - (by - ay) * (grid_li - ax)
        contrib = sign * cross
        field = contrib if field is None else np.minimum(field, contrib)
    assert field is not None
    return field


def _ring_area(xs: np.ndarray, ys: np.ndarray) -> float:
    if len(xs) < 3:
        return 0.0
    return float(abs(np.dot(xs, np.roll(ys, -1)) - np.dot(ys, np.roll(xs, -1))) / 2.0)


def _scalar_zero_cross(
    p0: tuple[float, float],
    v0: float,
    p1: tuple[float, float],
    v1: float,
) -> tuple[float, float]:
    """Linear edge crossing where scalar goes from v0 to v1 through zero."""
    if v0 == v1:
        return p0
    t = v0 / (v0 - v1)
    return (p0[0] + t * (p1[0] - p0[0]), p0[1] + t * (p1[1] - p0[1]))


def _clip_polygon_halfplane(
    verts: list[tuple[float, float]],
    scalar_fn: Callable[[float, float], float],
    *,
    keep_nonpositive: bool,
) -> list[tuple[float, float]]:
    """Sutherland–Hodgman clip: keep vertices where scalar <= 0 (or >= 0)."""
    if len(verts) < 3:
        return []

    def inside(val: float) -> bool:
        return val <= 0.0 if keep_nonpositive else val >= 0.0

    out: list[tuple[float, float]] = []
    for i in range(len(verts)):
        curr = verts[i]
        prev = verts[i - 1]
        vc = scalar_fn(curr[0], curr[1])
        vp = scalar_fn(prev[0], prev[1])
        curr_in = inside(vc)
        prev_in = inside(vp)
        if curr_in:
            if not prev_in:
                out.append(_scalar_zero_cross(prev, vp, curr, vc))
            out.append(curr)
        elif prev_in:
            out.append(_scalar_zero_cross(prev, vp, curr, vc))
    return out


def _water_band_scalars(params: GridJobParams) -> tuple[Callable[[float, float], float], Callable[[float, float], float]]:
    from ..phreeqc.gas_limits import water_gas_scalar

    def o2(ph: float, pe: float) -> float:
        return water_gas_scalar(
            "O2(g)", ph=ph, pe=pe, temp_c=params.temp_c, limit_atm=params.o2_limit_atm,
        )

    def h2(ph: float, pe: float) -> float:
        return water_gas_scalar(
            "H2(g)", ph=ph, pe=pe, temp_c=params.temp_c, limit_atm=params.h2_limit_atm,
        )

    return o2, h2


def _clip_polygon_to_water_band(
    xs: list[float],
    ys: list[float],
    params: GridJobParams,
) -> tuple[list[float], list[float]]:
    """Clip a contour ring to the analytic O₂/H₂ water-stability band."""
    if len(xs) < 3:
        return [], []
    o2_fn, h2_fn = _water_band_scalars(params)
    verts = list(zip(xs, ys))
    verts = _clip_polygon_halfplane(verts, o2_fn, keep_nonpositive=True)
    verts = _clip_polygon_halfplane(verts, h2_fn, keep_nonpositive=True)
    if len(verts) < 3:
        return [], []
    return [v[0] for v in verts], [v[1] for v in verts]


def _plot_box_polygon(
    ph_min: float, ph_max: float, pe_min: float, pe_max: float,
) -> list[tuple[float, float]]:
    return [
        (ph_min, pe_min),
        (ph_max, pe_min),
        (ph_max, pe_max),
        (ph_min, pe_max),
    ]


def _gas_outside_polygon(
    gas: str,
    params: GridJobParams,
    *,
    ph_min: float,
    ph_max: float,
    pe_min: float,
    pe_max: float,
) -> tuple[list[float], list[float]]:
    """Analytic fill for an O₂ or H₂ outside region (half-plane ∩ plot box)."""
    o2_fn, h2_fn = _water_band_scalars(params)
    scalar_fn = o2_fn if gas == "O2(g)" else h2_fn
    verts = _clip_polygon_halfplane(
        _plot_box_polygon(ph_min, ph_max, pe_min, pe_max),
        scalar_fn,
        keep_nonpositive=False,
    )
    if len(verts) < 3:
        return [], []
    return [v[0] for v in verts], [v[1] for v in verts]


def _segments_to_boundary_lines(segments: list[dict[str, Any]]) -> dict[str, list]:
    bx: list[float | None] = []
    by: list[float | None] = []
    for seg in segments:
        xs = seg.get("x") or []
        ys = seg.get("y") or []
        if not xs:
            continue
        bx.extend(float(v) for v in xs)
        by.extend(float(v) for v in ys)
        bx.append(None)
        by.append(None)
    return {"x": bx, "y": by}


def _clip_segments_to_sum_window(
    segments: list[dict[str, Any]], *, lower: float, upper: float
) -> list[dict[str, Any]]:
    """Clip polyline segments to ``lower <= pH + pe <= upper`` (Liang–Barsky).

    Chemistry boundaries are traced across the full pe range; portions outside
    the water-stability band must not be drawn into the white O₂/H₂ regions.
    """
    out: list[dict[str, Any]] = []
    for seg in segments:
        xs = seg.get("x") or []
        ys = seg.get("y") or []
        for k in range(len(xs) - 1):
            x0, y0 = float(xs[k]), float(ys[k])
            x1, y1 = float(xs[k + 1]), float(ys[k + 1])
            s0, s1 = x0 + y0, x1 + y1
            ds = s1 - s0
            t0, t1 = 0.0, 1.0
            ok = True
            for p, q in ((ds, upper - s0), (-ds, s0 - lower)):
                if p == 0.0:
                    if q < 0.0:
                        ok = False
                        break
                    continue
                r = q / p
                if p < 0.0:
                    t0 = max(t0, r)
                else:
                    t1 = min(t1, r)
            if not ok or t0 > t1:
                continue
            ax, ay = x0 + t0 * (x1 - x0), y0 + t0 * (y1 - y0)
            bx, by = x0 + t1 * (x1 - x0), y0 + t1 * (y1 - y0)
            out.append({"x": [ax, bx], "y": [ay, by]})
    return out


def _pack_one_layer(
    *,
    base_rows: list[dict],
    layer_nodes: dict[str, Any],
    cat_fn: Callable[[dict], str],
    params: GridJobParams,
    factor: int,
    n_ph_fine: int,
    n_pe_fine: int,
    fine_ph: np.ndarray,
    fine_pe: np.ndarray,
    ph_lookup: dict[float, int],
    pe_lookup: dict[float, int],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble one display layer: polygons from SDF contouring + boundary lines."""
    node_cats = layer_nodes.get("node_cat") or []
    cell_lines = layer_nodes.get("cell_lines") or []
    line_cat_names = {c for r in cell_lines for c in (r["pos"], r["neg"])}
    o2_label, h2_label = water_gas_outside_labels(params)
    names = sorted(
        {cat_fn(r) for r in base_rows}
        | set(node_cats)
        | line_cat_names
        | {o2_label, h2_label}
    )
    name_index = {name: i for i, name in enumerate(names)}
    none_idx = name_index.get("none", -999)
    o2_idx = name_index[o2_label]
    h2_idx = name_index[h2_label]

    base_idx = _base_index_grid(
        base_rows,
        n_ph=params.ph_levels,
        n_pe=params.pe_levels,
        ph_lookup=ph_lookup,
        pe_lookup=pe_lookup,
        cat_fn=cat_fn,
        name_index=name_index,
    )

    # Integer fine grid: coarse block fill, then sampled fallback nodes, then
    # clean-cell side classification (so category presence is exact everywhere).
    node_fi = layer_nodes.get("node_fi") or []
    node_fj = layer_nodes.get("node_fj") or []
    overrides = {
        (int(fi), int(fj)): name_index.get(cat, -1)
        for fi, fj, cat in zip(node_fi, node_fj, node_cats)
    }
    fine = _fine_index_grid(
        base_idx,
        factor=factor,
        n_ph_fine=n_ph_fine,
        n_pe_fine=n_pe_fine,
        overrides=overrides,
    )

    recs = _parse_cell_lines(cell_lines, name_index)
    cell_regions = (
        layer_nodes.get("cell_regions")
        or layer_nodes.get("cell_wedges")  # trace bundles before v20
        or []
    )
    regions_by_cat = _parse_cell_regions(cell_regions, name_index)
    grid_lj, grid_li = np.meshgrid(
        np.arange(factor + 1, dtype=float),
        np.arange(factor + 1, dtype=float),
        indexing="ij",
    )
    _apply_cell_lines_to_grid(
        fine, recs, factor=factor, grid_li=grid_li, grid_lj=grid_lj
    )
    fine = _despeckle(fine)
    chem_fine = fine.copy()

    o2_scalar, h2_scalar, inside_window = water_gas_scalar_grids(
        fine_ph, fine_pe, params
    )
    fine[np.asarray(o2_scalar > 0.0)] = o2_idx
    fine[np.asarray((h2_scalar > 0.0) & (o2_scalar <= 0.0))] = h2_idx

    ph_lo, ph_hi = float(fine_ph[0]), float(fine_ph[-1])
    pe_lo, pe_hi = float(fine_pe[0]), float(fine_pe[-1])

    # Group clean-cell dividing lines by the categories they bound, so each
    # category's fill field can be overwritten with a continuous signed distance.
    recs_by_cat: dict[int, list[tuple[dict[str, Any], float]]] = defaultdict(list)
    for r in recs:
        recs_by_cat[r["pos"]].append((r, 1.0))
        recs_by_cat[r["neg"]].append((r, -1.0))

    polygons: list[dict[str, Any]] = []
    inside_dist = np.minimum(-o2_scalar, -h2_scalar)
    for cat in range(len(names)):
        if cat == none_idx:
            continue
        if cat == o2_idx:
            cx, cy = _gas_outside_polygon(
                "O2(g)", params, ph_min=ph_lo, ph_max=ph_hi, pe_min=pe_lo, pe_max=pe_hi,
            )
            if len(cx) >= 3:
                polygons.append({
                    "cat": cat,
                    "area": _ring_area(np.array(cx), np.array(cy)),
                    "x": cx,
                    "y": cy,
                })
            continue
        if cat == h2_idx:
            cx, cy = _gas_outside_polygon(
                "H2(g)", params, ph_min=ph_lo, ph_max=ph_hi, pe_min=pe_lo, pe_max=pe_hi,
            )
            if len(cx) >= 3:
                polygons.append({
                    "cat": cat,
                    "area": _ring_area(np.array(cx), np.array(cy)),
                    "x": cx,
                    "y": cy,
                })
            continue
        field = np.where(chem_fine == cat, _FIELD_BIG, -_FIELD_BIG).astype(float)
        # Exact per-cell contributions replace the raster init in their block.
        exact: dict[tuple[int, int], np.ndarray] = {}
        for r, sign in recs_by_cat.get(cat, ()):
            s = sign * ((grid_li - r["x1"]) * r["nx"] + (grid_lj - r["y1"]) * r["ny"])
            key = (r["i"], r["j"])
            exact[key] = s if key not in exact else np.maximum(exact[key], s)
        for region in regions_by_cat.get(cat, ()):
            block = _region_field_block(region, grid_li, grid_lj)
            key = (region["i"], region["j"])
            exact[key] = block if key not in exact else np.maximum(exact[key], block)
        for (ci, cj), block in exact.items():
            fi0, fj0 = ci * factor, cj * factor
            fi1 = min(fi0 + factor + 1, n_ph_fine)
            fj1 = min(fj0 + factor + 1, n_pe_fine)
            field[fj0:fj1, fi0:fi1] = block[: fj1 - fj0, : fi1 - fi0]
        field = np.minimum(field, inside_dist)
        if not (field > 0.0).any():
            continue
        for xs, ys in _field_contours(field, fine_ph, fine_pe):
            cx, cy = _clip_polygon_to_water_band(
                [float(v) for v in xs], [float(v) for v in ys], params,
            )
            if len(cx) < 3:
                continue
            polygons.append(
                {
                    "cat": cat,
                    "area": _ring_area(np.array(cx), np.array(cy)),
                    "x": cx,
                    "y": cy,
                }
            )

    # Boundary lines: traced chemistry segments (clipped to the water window so
    # they don't bleed into the O₂/H₂ regions) plus the analytic gas-limit lines.
    lower_sum, upper_sum = water_gas_sum_window(params)
    chem_segments = _clip_segments_to_sum_window(
        list(layer_nodes.get("boundaries") or []),
        lower=lower_sum,
        upper=upper_sum,
    )
    chem_segments.extend(
        water_gas_boundary_segments(
            params,
            ph_min=float(fine_ph[0]),
            ph_max=float(fine_ph[-1]),
            pe_min=float(fine_pe[0]),
            pe_max=float(fine_pe[-1]),
        )
    )
    boundaries = _segments_to_boundary_lines(chem_segments)

    layer: dict[str, Any] = {
        "names": names,
        "polygons": polygons,
        "boundaries": boundaries,
        "plot_ph_span": float(fine_ph[-1] - fine_ph[0]),
        "plot_pe_span": float(fine_pe[-1] - fine_pe[0]),
    }
    if extra:
        layer.update(extra)
    return layer


def pack_traced_display(
    params: GridJobParams,
    base_rows: list[dict],
    trace_bundle: dict[str, Any],
    *,
    db_path: str,
    progress_cb: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Build all display layers from a boundary-trace bundle.

    Each layer gets smooth fill polygons (SDF contouring) and thin boundary
    polylines. The base grid rows supply hover data only; colors come from the
    traced geometry in ``trace_bundle``.
    """
    trace_layers = trace_bundle.get("layers") or {}
    factor = int(trace_bundle.get("refine_factor") or 1)
    n_ph_fine = fine_axis_levels(params.ph_levels, factor)
    n_pe_fine = fine_axis_levels(params.pe_levels, factor)
    fine_ph = np.linspace(params.ph_min, params.ph_max, n_ph_fine)
    fine_pe = np.linspace(params.pe_min, params.pe_max, n_pe_fine)

    base_ph = np.linspace(params.ph_min, params.ph_max, params.ph_levels)
    base_pe = np.linspace(params.pe_min, params.pe_max, params.pe_levels)
    ph_lookup = {round(float(v), 12): i for i, v in enumerate(base_ph)}
    pe_lookup = {round(float(v), 12): i for i, v in enumerate(base_pe)}

    collisions = frozenset(params.solid_aqueous_collisions)
    subset_map = params.phase_names_by_subset
    job_phases = params.phases
    solid_set = set(job_phases)
    subset_list = subsets_for_job(params)
    pack_steps = count_layer_pack_steps(params)
    step = 0

    def tick() -> None:
        nonlocal step
        step += 1
        if progress_cb:
            progress_cb(step, pack_steps)

    common = dict(
        base_rows=base_rows,
        params=params,
        factor=factor,
        n_ph_fine=n_ph_fine,
        n_pe_fine=n_pe_fine,
        fine_ph=fine_ph,
        fine_pe=fine_pe,
        ph_lookup=ph_lookup,
        pe_lookup=pe_lookup,
    )

    solid_subsets: dict[str, Any] = {}
    aqueous_subsets: dict[str, Any] = {}
    if params.layer_solids:
        for subset in subset_list:
            key = subset_key(subset)
            eligible = frozenset(subset_map.get(key, ()))

            def cat_fn(row: dict, subset: tuple[str, ...] = subset, elig: frozenset[str] = eligible) -> str:
                return category_solid_subset(
                    row, subset, eligible_phases=elig,
                    job_phases=job_phases, collision_names=collisions,
                )

            _layer = trace_layers.get(f"solid:{key}", {})
            names_preview = sorted(
                {cat_fn(r) for r in base_rows}
                | set(_layer.get("node_cat") or [])
                | {c for r in (_layer.get("cell_lines") or []) for c in (r["pos"], r["neg"])}
            )
            solid_subsets[key] = _pack_one_layer(
                layer_nodes=trace_layers.get(f"solid:{key}", {}),
                cat_fn=cat_fn,
                extra={
                    "elements": list(subset),
                    "aqueous_names": [
                        n for n in names_preview
                        if not label_is_solid(n, solid_set, collisions)
                    ],
                },
                **common,
            )
            tick()

    if params.layer_aqueous:
        for subset in subset_list:
            key = subset_key(subset)

            def aq_cat_fn(row: dict, s: set[str] = set(subset)) -> str:
                return dominant_aq_species_subset(row, s)

            aqueous_subsets[key] = _pack_one_layer(
                layer_nodes=trace_layers.get(f"aqueous:{key}", {}),
                cat_fn=aq_cat_fn,
                extra={"elements": list(subset)},
                **common,
            )
            tick()

    stability_segments = (trace_bundle.get("stability_limits") or {}).get("segments") or []

    return {
        "mode": "traced",
        "factor": factor,
        "interpolated": True,
        "stability_limits": _segments_to_boundary_lines(stability_segments),
        "layers": {
            "solid_subsets": solid_subsets,
            "aqueous_subsets": aqueous_subsets,
        },
    }
