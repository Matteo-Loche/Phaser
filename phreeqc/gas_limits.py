"""Water-stability and component-gas limit tracing for pe–pH diagrams.

Water limits (O₂/H₂) use analytic fugacity from pe/pH/temperature.
Component gases (CO₂, CH₄, …) use ``SI(gas) - log10(P_ref)`` from grid rows,
refined along cell edges with the same Brent root-finder as phase boundaries.
"""
from __future__ import annotations

import math
from typing import Any, Callable

import numpy as np
from scipy.optimize import brentq

from .. import config
from .boundary_trace import PointEvaluator, _edge_coords
from .engine import GridJobParams
from .sweep import _point_key

_WATER_GASES = ("O2(g)", "H2(g)")


def water_stability_limits_enabled(params: GridJobParams) -> bool:
    """Whether analytic O₂/H₂ water-stability overlays apply for this job."""
    return params.solution_mode != "direct"


def log_k_o2_water(*, temp_c: float = 25.0) -> float:
    """log K for O2(g) + 4H+ + 4e- = 2H2O at temperature T (°C).

    Anchored at 25 °C (≈20.75); linear d(log K)/dT approximation for prototype.
    """
    return 20.75 + 0.0018 * (temp_c - 25.0)


def log_f_o2(*, ph: float, pe: float, temp_c: float) -> float:
    """log10(fugacity O2 in atm) from pe/pH."""
    return 4.0 * (pe + ph - log_k_o2_water(temp_c=temp_c))


def log_f_h2(*, ph: float, pe: float, temp_c: float) -> float:
    """log10(fugacity H2 in atm) from pe/pH (log K ≈ 0 at 25 °C)."""
    del temp_c
    return -2.0 * (pe + ph)


def water_gas_scalar(
    gas: str,
    *,
    ph: float,
    pe: float,
    temp_c: float,
    limit_atm: float,
) -> float:
    """Zero on the limit line; positive outside (supersaturated / infeasible)."""
    if gas == "O2(g)":
        log_f = log_f_o2(ph=ph, pe=pe, temp_c=temp_c)
    elif gas == "H2(g)":
        log_f = log_f_h2(ph=ph, pe=pe, temp_c=temp_c)
    else:
        raise ValueError(gas)
    return log_f - math.log10(limit_atm)


def component_gas_scalar(row: dict, gas: str, *, limit_atm: float) -> float | None:
    """SI(gas) - log10(P_ref); SI is log10(fugacity) for gas phases in PHREEQC."""
    si = (row.get("gas_si") or {}).get(gas)
    if si is None:
        si = (row.get("si") or {}).get(gas)
    if si is None or si != si:
        return None
    return float(si) - math.log10(limit_atm)


def water_gas_outside_labels(params: GridJobParams) -> tuple[str, str]:
    """Display labels for regions outside the water-stability window."""
    return (
        f"O2(g) > {params.o2_limit_atm:g} atm",
        f"H2(g) > {params.h2_limit_atm:g} atm",
    )


def water_gas_scalar_grids(
    fine_ph: np.ndarray,
    fine_pe: np.ndarray,
    params: GridJobParams,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Signed-distance scalars for O₂/H₂ limits on the fine raster (pe × pH)."""
    pe_grid, ph_grid = np.meshgrid(fine_pe, fine_ph, indexing="ij")
    k_o2 = log_k_o2_water(temp_c=params.temp_c)
    log_o2_lim = math.log10(params.o2_limit_atm)
    log_h2_lim = math.log10(params.h2_limit_atm)
    o2 = 4.0 * (pe_grid + ph_grid - k_o2) - log_o2_lim
    h2 = -2.0 * (pe_grid + ph_grid) - log_h2_lim
    inside = (o2 <= 0.0) & (h2 <= 0.0)
    return o2, h2, inside


def water_gas_sum_window(params: GridJobParams) -> tuple[float, float]:
    """``(lower, upper)`` bounds on ``pe + pH`` for the water-stability band.

    * upper (O₂): ``pe + pH <= K_O2 + log10(fO2_limit)/4``
    * lower (H₂): ``pe + pH >= -log10(fH2_limit)/2``
    """
    upper = log_k_o2_water(temp_c=params.temp_c) + math.log10(params.o2_limit_atm) / 4.0
    lower = -math.log10(params.h2_limit_atm) / 2.0
    return lower, upper


def water_gas_boundary_segments(
    params: GridJobParams,
    *,
    ph_min: float,
    ph_max: float,
    pe_min: float,
    pe_max: float,
) -> list[dict[str, Any]]:
    """Clipped O₂/H₂ lines as boundary vectors for the display frame."""
    segments: list[dict[str, Any]] = []
    for gas, limit_atm in (
        ("O2(g)", params.o2_limit_atm),
        ("H2(g)", params.h2_limit_atm),
    ):
        line = _water_gas_limit_line(
            gas,
            temp_c=params.temp_c,
            limit_atm=limit_atm,
            ph0=ph_min,
            ph1=ph_max,
            pe0=pe_min,
            pe1=pe_max,
        )
        if line is None:
            continue
        x0, y0, x1, y1 = line
        segments.append({"x": [x0, x1], "y": [y0, y1]})
    return segments


def water_gas_domain_labels(
    *,
    ph: float,
    pe: float,
    temp_c: float,
    o2_limit_atm: float,
    h2_limit_atm: float,
) -> dict[str, str]:
    """PhreePlot-style outside labels for water-gas limits."""
    out: dict[str, str] = {}
    if water_gas_scalar("O2(g)", ph=ph, pe=pe, temp_c=temp_c, limit_atm=o2_limit_atm) > 0:
        out["O2(g)"] = f"O2(g) > {o2_limit_atm:g} atm"
    if water_gas_scalar("H2(g)", ph=ph, pe=pe, temp_c=temp_c, limit_atm=h2_limit_atm) > 0:
        out["H2(g)"] = f"H2(g) > {h2_limit_atm:g} atm"
    return out


def _interp(t: float, ph0: float, pe0: float, ph1: float, pe1: float) -> tuple[float, float]:
    return ph0 + t * (ph1 - ph0), pe0 + t * (pe1 - pe0)


def _water_gas_limit_line(
    gas: str,
    *,
    temp_c: float,
    limit_atm: float,
    ph0: float,
    ph1: float,
    pe0: float,
    pe1: float,
) -> tuple[float, float, float, float] | None:
    """Analytic water-stability line ``pe = -pH + b`` clipped to the axis box.

    Both O₂ and H₂ limits are straight lines of slope -1 in (pH, pe):

    * O₂(g): ``4(pe+pH-K) = log10(limit)`` → ``pe = -pH + K + log10(limit)/4``
    * H₂(g): ``-2(pe+pH) = log10(limit)`` → ``pe = -pH - log10(limit)/2``

    Returns a single ``(x0, y0, x1, y1)`` boundary vector (not per-cell pieces),
    or ``None`` when the line does not intersect the plotted rectangle.
    """
    if gas == "O2(g)":
        intercept = log_k_o2_water(temp_c=temp_c) + math.log10(limit_atm) / 4.0
    elif gas == "H2(g)":
        intercept = -math.log10(limit_atm) / 2.0
    else:
        raise ValueError(gas)

    def pe_at(ph: float) -> float:
        return -ph + intercept

    eps = 1e-9
    # Candidate pH values: box pH edges and where the line meets the pe edges.
    candidates = sorted({ph0, ph1, intercept - pe0, intercept - pe1})
    inside = [
        p for p in candidates
        if ph0 - eps <= p <= ph1 + eps and pe0 - eps <= pe_at(p) <= pe1 + eps
    ]
    if len(inside) < 2:
        return None
    a, b = min(inside), max(inside)
    if abs(b - a) < eps:
        return None
    return (a, pe_at(a), b, pe_at(b))


def _trace_component_gas_edges(
    evaluator: PointEvaluator,
    i: int,
    j: int,
    base_ph: np.ndarray,
    base_pe: np.ndarray,
    base_ij: dict[tuple[int, int], Any],
    *,
    gas: str,
    limit_atm: float,
    tol: float,
) -> list[dict[str, Any]]:
    from dataclasses import asdict

    def row_at(ii: int, jj: int) -> dict:
        r = base_ij[(ii, jj)]
        return r if isinstance(r, dict) else asdict(r)

    corners = [
        row_at(i, j),
        row_at(i + 1, j),
        row_at(i + 1, j + 1),
        row_at(i, j + 1),
    ]
    vals = [component_gas_scalar(r, gas, limit_atm=limit_atm) for r in corners]
    if any(v is None for v in vals):
        return []

    points: list[tuple[float, float]] = []
    for edge in range(4):
        v0, v1 = vals[edge], vals[(edge + 1) % 4]
        if v0 is None or v1 is None or v0 * v1 > 0:
            continue
        ph0, pe0, ph1, pe1 = _edge_coords(i, j, edge, base_ph, base_pe)

        def f(t: float) -> float:
            ph = ph0 + t * (ph1 - ph0)
            pe = pe0 + t * (pe1 - pe0)
            row = evaluator.eval(ph, pe)
            val = component_gas_scalar(row, gas, limit_atm=limit_atm)
            if val is None:
                raise ValueError("lost gas bracket")
            return val

        try:
            t = float(brentq(f, 0.0, 1.0, xtol=tol, rtol=tol))
        except (ValueError, RuntimeError):
            continue
        points.append(_interp(t, ph0, pe0, ph1, pe1))

    if len(points) < 2:
        return []
    (x0, y0), (x1, y1) = points[0], points[-1]
    return [{
        "kind": "gas_limit",
        "gas": gas,
        "limit_atm": limit_atm,
        "style": "component",
        "x": [x0, x1],
        "y": [y0, y1],
    }]


def trace_gas_limit_segments(
    params: GridJobParams,
    *,
    base_ph: np.ndarray,
    base_pe: np.ndarray,
    base_ij: dict[tuple[int, int], Any] | None = None,
    evaluator: PointEvaluator | None = None,
    tolerance: float | None = None,
) -> list[dict[str, Any]]:
    """Trace O₂/H₂ water limits (all cells) and component gas limits."""
    tol = tolerance or config.BOUNDARY_TRACE_TOLERANCE
    n_ph, n_pe = len(base_ph), len(base_pe)
    if n_ph < 2 or n_pe < 2:
        return []

    segments: list[dict[str, Any]] = []
    if water_stability_limits_enabled(params):
        ph_lo, ph_hi = float(base_ph[0]), float(base_ph[-1])
        pe_lo, pe_hi = float(base_pe[0]), float(base_pe[-1])
        for gas, limit_atm in (
            ("O2(g)", params.o2_limit_atm),
            ("H2(g)", params.h2_limit_atm),
        ):
            line = _water_gas_limit_line(
                gas, temp_c=params.temp_c, limit_atm=limit_atm,
                ph0=ph_lo, ph1=ph_hi, pe0=pe_lo, pe1=pe_hi,
            )
            if line is None:
                continue
            x0, y0, x1, y1 = line
            segments.append({
                "kind": "gas_limit",
                "gas": gas,
                "limit_atm": limit_atm,
                "style": "water",
                "x": [x0, x1],
                "y": [y0, y1],
            })

    component_gases = tuple(
        g for g in params.trace_gas_phases if g not in _WATER_GASES
    )
    if component_gases and base_ij is not None:
        if evaluator is None:
            from dataclasses import asdict
            seed: dict[tuple[float, float], dict] = {}
            for r in base_ij.values():
                d = r if isinstance(r, dict) else asdict(r)
                seed[_point_key(float(d["ph"]), float(d["pe"]))] = d
            evaluator = PointEvaluator(params, seed)
        for gas in component_gases:
            for j in range(n_pe - 1):
                for i in range(n_ph - 1):
                    segments.extend(
                        _trace_component_gas_edges(
                            evaluator, i, j, base_ph, base_pe, base_ij,
                            gas=gas, limit_atm=params.component_gas_limit_atm, tol=tol,
                        )
                    )
    return segments
