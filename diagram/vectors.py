"""Vector display layers from adaptive boundary refinement.

Only the base grid is packed for hover/data (see ``packer.py``). For *display*,
this module builds ONE fine category grid (interior block-filled from the base
grid, boundary cells overlaid with refined PHREEQC nodes) and then uses
marching-squares contours (``skimage.measure.find_contours``) to produce:

- **fills**: filled contour rings (smooth region outlines, no raster, no
  seams — a single coordinate system is used throughout). Rings are emitted with
  their area so the browser can paint large regions first and enclosed regions
  afterward, including white ``none`` holes.
- **boundaries**: the same contour polylines, drawn as thin smooth lines.

Marching squares places vertices on cell edges by interpolation, so boundaries
are real curves rather than axis-aligned staircases.
"""
from __future__ import annotations

from typing import Any, Callable

import numpy as np
from skimage.measure import find_contours

from ..phreeqc.engine import GridJobParams
from .packer import category_solid_subset, subset_key, subsets_to_pack
from .phases import phase_element_map


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
    refined_idx: dict[tuple[int, int], int],
) -> np.ndarray:
    n_pe, n_ph = base_idx.shape
    fi_to_bi = np.clip(np.round(np.arange(n_ph_fine) / factor).astype(int), 0, n_ph - 1)
    fj_to_bj = np.clip(np.round(np.arange(n_pe_fine) / factor).astype(int), 0, n_pe - 1)
    fine = base_idx[np.ix_(fj_to_bj, fi_to_bi)].copy()
    for (fi, fj), cat in refined_idx.items():
        if 0 <= fj < n_pe_fine and 0 <= fi < n_ph_fine:
            fine[fj, fi] = cat
    return fine


def _mask_contours(
    mask: np.ndarray,
    fine_ph: np.ndarray,
    fine_pe: np.ndarray,
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Marching-squares contours of a binary mask, mapped to (pH, pe) coords.

    The mask is zero-padded so regions touching the plot edge close cleanly on
    the true axis bounds.
    """
    n_pe, n_ph = mask.shape
    padded = np.pad(mask.astype(float), 1)
    rings = find_contours(padded, 0.5)
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


def _ring_area(xs: np.ndarray, ys: np.ndarray) -> float:
    """Absolute shoelace area for draw ordering of contour rings."""
    if len(xs) < 3:
        return 0.0
    return float(abs(np.dot(xs, np.roll(ys, -1)) - np.dot(ys, np.roll(xs, -1))) / 2.0)


def _pack_one_layer(
    *,
    base_rows: list[dict],
    refine_rows_by_node: dict[tuple[int, int], dict],
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
    names: list[str] = sorted(
        {cat_fn(r) for r in base_rows} | {cat_fn(r) for r in refine_rows_by_node.values()}
    )
    name_index = {name: i for i, name in enumerate(names)}
    none_idx = name_index.get("none", -999)

    base_idx = _base_index_grid(
        base_rows,
        n_ph=params.ph_levels,
        n_pe=params.pe_levels,
        ph_lookup=ph_lookup,
        pe_lookup=pe_lookup,
        cat_fn=cat_fn,
        name_index=name_index,
    )
    refined_idx = {
        node: name_index.get(cat_fn(row), -1) for node, row in refine_rows_by_node.items()
    }
    fine = _fine_index_grid(
        base_idx,
        factor=factor,
        n_ph_fine=n_ph_fine,
        n_pe_fine=n_pe_fine,
        refined_idx=refined_idx,
    )

    polygons: list[dict[str, Any]] = []
    bx: list[float | None] = []
    by: list[float | None] = []
    for cat in range(len(names)):
        mask = fine == cat
        if not mask.any():
            continue
        for xs, ys in _mask_contours(mask, fine_ph, fine_pe):
            area = _ring_area(xs, ys)
            polygons.append(
                {
                    "cat": cat,
                    "area": area,
                    "x": [float(v) for v in xs],
                    "y": [float(v) for v in ys],
                }
            )
            if cat != none_idx:
                bx.extend(float(v) for v in xs)
                bx.append(None)
                by.extend(float(v) for v in ys)
                by.append(None)

    layer: dict[str, Any] = {
        "names": names,
        "polygons": polygons,
        "boundaries": {"x": bx, "y": by},
    }
    if extra:
        layer.update(extra)
    return layer


def pack_adaptive_display(
    params: GridJobParams,
    base_rows: list[dict],
    refine_bundle: dict[str, Any],
    *,
    db_path: str,
    progress_cb: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    factor = int(refine_bundle["factor"])
    fine_ph = np.asarray(refine_bundle["fine_ph"], dtype=float)
    fine_pe = np.asarray(refine_bundle["fine_pe"], dtype=float)
    n_ph_fine = len(fine_ph)
    n_pe_fine = len(fine_pe)

    refine_rows_by_node: dict[tuple[int, int], dict] = {}
    for item in refine_bundle["evaluated"]:
        refine_rows_by_node[(int(item["fi"]), int(item["fj"]))] = item["row"]

    base_ph = np.linspace(params.ph_min, params.ph_max, params.ph_levels)
    base_pe = np.linspace(params.pe_min, params.pe_max, params.pe_levels)
    ph_lookup = {round(float(v), 12): i for i, v in enumerate(base_ph)}
    pe_lookup = {round(float(v), 12): i for i, v in enumerate(base_pe)}

    phase_elements = phase_element_map(db_path)
    job_phases = params.phases
    subset_list = subsets_to_pack(params.system_elements)
    pack_steps = len(subset_list) + len(params.system_elements)
    step = 0

    def tick() -> None:
        nonlocal step
        step += 1
        if progress_cb:
            progress_cb(step, pack_steps)

    common = dict(
        base_rows=base_rows,
        refine_rows_by_node=refine_rows_by_node,
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
    for subset in subset_list:
        key = subset_key(subset)

        def cat_fn(row: dict, subset: tuple[str, ...] = subset) -> str:
            return category_solid_subset(
                row, subset, phase_elements=phase_elements, job_phases=job_phases
            )

        solid_set = set(job_phases)
        names_preview = sorted(
            {cat_fn(r) for r in base_rows}
            | {cat_fn(r) for r in refine_rows_by_node.values()}
        )
        solid_subsets[key] = _pack_one_layer(
            cat_fn=cat_fn,
            extra={
                "elements": list(subset),
                "aqueous_names": [n for n in names_preview if n not in solid_set],
            },
            **common,
        )
        tick()

    elements: dict[str, Any] = {}
    for elem in params.system_elements:
        def elem_cat(row: dict, e: str = elem) -> str:
            return row.get("dominant_aq_by_element", {}).get(e, "none") or "none"

        elements[elem] = _pack_one_layer(cat_fn=elem_cat, **common)
        tick()

    return {
        "mode": "vectors",
        "factor": factor,
        "interpolated": True,
        "layers": {
            "solid_subsets": solid_subsets,
            "elements": elements,
        },
    }
