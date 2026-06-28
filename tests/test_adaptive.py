"""Unit tests for adaptive boundary refinement."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PHASER.phreeqc.adaptive import (
    boundary_base_cells,
    choose_refine_factor,
    estimate_adaptive_points,
    fine_axis_levels,
    fine_nodes_for_cells,
    layer_signature_fn,
)
from PHASER.diagram import phases as _phases_mod
from PHASER.phreeqc.engine import GridJobParams


def test_fine_axis_levels_aligns_base_nodes():
    # Base nodes must land exactly on fine nodes (step = factor).
    assert fine_axis_levels(100, 4) == 397
    assert fine_axis_levels(3, 4) == 9
    assert fine_axis_levels(1, 4) == 1
    assert fine_axis_levels(100, 1) == 100


def test_boundary_cells_detected_only_where_corners_differ():
    # categories[j, i]; a single B in the corner makes 1 boundary cell.
    cat = np.array(
        [
            ["A", "A", "A"],
            ["A", "A", "B"],
        ],
        dtype=object,
    )
    cells = boundary_base_cells(cat)
    # Only the right cell (i=1, j=0) touches the B corner.
    assert cells == [(1, 0)]


def test_homogeneous_grid_has_no_boundary_cells():
    cat = np.full((4, 4), "A", dtype=object)
    assert boundary_base_cells(cat) == []


def test_fine_nodes_cover_subdivided_cell():
    nodes = fine_nodes_for_cells([(0, 0)], factor=4, n_ph_fine=9, n_pe_fine=9)
    # A single base cell spans a (factor+1) x (factor+1) block of fine nodes.
    assert len(nodes) == 25
    assert (0, 0) in nodes
    assert (4, 4) in nodes
    assert (5, 5) not in nodes


def test_choose_refine_factor_respects_budget():
    # Tiny budget forces no refinement.
    assert choose_refine_factor(100, 100, boundary_cell_count=1000, desired_factor=4, budget=10) == 1
    # Generous budget keeps the desired factor.
    assert choose_refine_factor(100, 100, boundary_cell_count=200, desired_factor=4, budget=40000) == 4


def test_estimate_includes_base_grid():
    est = estimate_adaptive_points(100, 100, refine_factor=4)
    assert est >= 100 * 100
    assert est <= 40000


def test_signature_detects_subset_boundary_when_full_system_solid_is_constant(monkeypatch):
    # FeSolid always dominant for the full system; CuSolid only supersaturated
    # for pe > 0 -> the Cu-only subset layer has a boundary the full-system
    # dominant_solid does not.
    monkeypatch.setattr(
        _phases_mod,
        "phase_element_map",
        lambda db: {"FeSolid": frozenset({"Fe"}), "CuSolid": frozenset({"Cu"})},
    )
    params = GridJobParams(
        db_path="x", dll_path="y", temp_c=25,
        ph_min=2, ph_max=12, ph_levels=3,
        pe_min=-10, pe_max=14, pe_levels=3,
        totals={"Fe": 1.0, "Cu": 1.0},
        phases=("FeSolid", "CuSolid"),
        system_elements=("Cu", "Fe"),
    )
    sig = layer_signature_fn(params)
    below = sig({"converged": True, "si": {"FeSolid": 1.0, "CuSolid": -1.0},
                 "dominant_solid": "FeSolid", "dominant_aq_by_element": {},
                 "aq_molality_by_element": {}})
    above = sig({"converged": True, "si": {"FeSolid": 1.0, "CuSolid": 1.0},
                 "dominant_solid": "FeSolid", "dominant_aq_by_element": {},
                 "aq_molality_by_element": {}})
    assert below != above  # subset layer change is visible in the signature
