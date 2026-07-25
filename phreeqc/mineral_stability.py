"""Mineral-stability categories and root-finding scalars.

Two assemblage (EQUILIBRIUM_PHASES) category modes:

- ``moles`` — argmax precipitated moles (mineral predominance)
- ``costability`` — all solids with moles > 0 joined (post-precip co-stability;
  equivalent to phases held at SI ≈ 0 by EQUI)

Legacy SI predominance (``dummy_titration`` / ``titration``) is untouched —
these helpers are not used on that path.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal

from ..diagram.packer import (
    dominant_aq_in_subset,
    dominant_aq_species_subset,
    label_is_solid,
    phase_from_label,
    solid_label,
    subset_key,
    subsets_for_job,
)
from .catalog import is_gas

MineralCategoryMode = Literal["moles", "costability"]

# Primary solid presence floor (moles). Below this, treat as absent.
DEFAULT_PRECIP_EPS = 1e-16
# Relative tolerance for equal-mole co-precip ties vs the max (moles mode only).
DEFAULT_TIE_REL = 1e-9
_MOL_FLOOR = 1e-30


def _normalize_category_mode(mode: str) -> Literal["moles", "costability"]:
    if mode == "costability":
        return "costability"
    return "moles"


def precip_moles_in_subset(
    phase_moles: Mapping[str, float],
    eligible: set[str] | frozenset[str] | Sequence[str],
) -> dict[str, float]:
    """Filter ``phase_moles`` to eligible non-gas solids with finite values."""
    allow = frozenset(eligible)
    out: dict[str, float] = {}
    for name, moles in phase_moles.items():
        if name not in allow or is_gas(name):
            continue
        if moles != moles:
            continue
        out[name] = float(moles)
    return out


def dominant_precip_label(
    phase_moles: Mapping[str, float],
    eligible: set[str] | frozenset[str] | Sequence[str],
    *,
    collision_names: frozenset[str] = frozenset(),
    eps: float = DEFAULT_PRECIP_EPS,
    tie_rel: float = DEFAULT_TIE_REL,
    default: str = "aqueous",
) -> str:
    """Argmax precipitated moles; equal moles → ``\"A + B\"``; none → ``default``.

    When two (or more) phases tie within ``max(eps, tie_rel * max_moles)`` of
    the maximum and that maximum exceeds ``eps``, return a sorted ``" + "``
    join of their solid labels. v1 only treats near-equal moles as co-precip.
    """
    finite = precip_moles_in_subset(phase_moles, eligible)
    if not finite:
        return default
    max_m = max(finite.values())
    if max_m <= eps:
        return default
    tol = max(eps, tie_rel * max_m)
    tied = sorted(p for p, m in finite.items() if abs(m - max_m) <= tol)
    if len(tied) >= 2:
        return " + ".join(solid_label(p, collision_names) for p in tied)
    return solid_label(tied[0], collision_names)


def costability_label(
    phase_moles: Mapping[str, float],
    eligible: set[str] | frozenset[str] | Sequence[str],
    *,
    collision_names: frozenset[str] = frozenset(),
    eps: float = DEFAULT_PRECIP_EPS,
    default: str = "aqueous",
) -> str:
    """All solids with precipitated moles > ``eps``, sorted ``\"A + B\"``.

    Post-EQUI co-stability: phases held at SI ≈ 0 are exactly those with
    moles > 0. Not free-SI supersaturation and not argmax predominance.
    """
    finite = precip_moles_in_subset(phase_moles, eligible)
    present = sorted(p for p, m in finite.items() if m > eps)
    if not present:
        return default
    return " + ".join(solid_label(p, collision_names) for p in present)


def category_precip_subset(
    row: Mapping[str, Any],
    subset: tuple[str, ...],
    *,
    eligible_phases: frozenset[str],
    job_phases: tuple[str, ...],
    collision_names: frozenset[str] = frozenset(),
    eps: float = DEFAULT_PRECIP_EPS,
    tie_rel: float = DEFAULT_TIE_REL,
) -> str:
    """Mineral-stability category for one element subset (argmax moles).

    If no solid has precipitated above ``eps``, fall back to the dominant
    aqueous species in the subset.
    """
    synth = row.get("synthetic_label")
    if synth:
        return str(synth)
    eligible = {
        p for p in job_phases if p in eligible_phases and not is_gas(p)
    }
    label = dominant_precip_label(
        row.get("phase_moles") or {},
        eligible,
        collision_names=collision_names,
        eps=eps,
        tie_rel=tie_rel,
        default="",
    )
    if label:
        return label
    return dominant_aq_in_subset(dict(row), set(subset))


def category_costability_subset(
    row: Mapping[str, Any],
    subset: tuple[str, ...],
    *,
    eligible_phases: frozenset[str],
    job_phases: tuple[str, ...],
    collision_names: frozenset[str] = frozenset(),
    eps: float = DEFAULT_PRECIP_EPS,
) -> str:
    """Post-precip co-stability: join every solid with moles > ``eps``.

    Requires assemblage ``phase_moles``. Falls back to dominant aqueous when
    nothing has precipitated.
    """
    synth = row.get("synthetic_label")
    if synth:
        return str(synth)
    eligible = {
        p for p in job_phases if p in eligible_phases and not is_gas(p)
    }
    label = costability_label(
        row.get("phase_moles") or {},
        eligible,
        collision_names=collision_names,
        eps=eps,
        default="",
    )
    if label:
        return label
    return dominant_aq_in_subset(dict(row), set(subset))


def mineral_stability_signature(
    row: Mapping[str, Any],
    *,
    subsets: Sequence[tuple[str, ...]],
    eligible_by_subset: Sequence[frozenset[str]] | None = None,
    job_phases: tuple[str, ...] = (),
    collision_names: frozenset[str] = frozenset(),
    layer_solids: bool = True,
    layer_aqueous: bool = False,
    category_mode: MineralCategoryMode = "moles",
    eps: float = DEFAULT_PRECIP_EPS,
    tie_rel: float = DEFAULT_TIE_REL,
) -> tuple[str, ...]:
    """Tuple of category parts for adaptive-style cell signatures."""
    mode = _normalize_category_mode(category_mode)
    synth = row.get("synthetic_label")
    n_parts = (len(subsets) if layer_solids else 0) + (
        len(subsets) if layer_aqueous else 0
    )
    if synth:
        return (str(synth),) * n_parts if n_parts else (str(synth),)
    if not row.get("converged"):
        return ("__none__",)

    if eligible_by_subset is None:
        eligible_by_subset = tuple(
            frozenset(p for p in job_phases if not is_gas(p)) for _ in subsets
        )

    parts: list[str] = []
    if layer_solids:
        for subset, elig in zip(subsets, eligible_by_subset):
            if mode == "costability":
                parts.append(
                    category_costability_subset(
                        row,
                        subset,
                        eligible_phases=elig,
                        job_phases=job_phases,
                        collision_names=collision_names,
                        eps=eps,
                    )
                )
            else:
                parts.append(
                    category_precip_subset(
                        row,
                        subset,
                        eligible_phases=elig,
                        job_phases=job_phases,
                        collision_names=collision_names,
                        eps=eps,
                        tie_rel=tie_rel,
                    )
                )
    if layer_aqueous:
        for subset in subsets:
            parts.append(dominant_aq_species_subset(dict(row), set(subset)))
    return tuple(parts)


def mineral_stability_signature_fn(
    params,
    *,
    category_mode: MineralCategoryMode = "moles",
) -> Callable[[dict], tuple]:
    """Build a row → signature callable from ``GridJobParams``."""
    collisions = frozenset(params.solid_aqueous_collisions)
    subset_map = params.phase_names_by_subset
    subsets = subsets_for_job(params)
    eligible_by_subset: list[frozenset[str]] = []
    for subset in subsets:
        elig = frozenset(
            p
            for p in params.phases
            if not is_gas(p) and p in subset_map.get(subset_key(subset), ())
        )
        eligible_by_subset.append(elig)

    def signature(row: dict) -> tuple:
        return mineral_stability_signature(
            row,
            subsets=subsets,
            eligible_by_subset=eligible_by_subset,
            job_phases=params.phases,
            collision_names=collisions,
            layer_solids=params.layer_solids,
            layer_aqueous=params.layer_aqueous,
            category_mode=category_mode,
        )

    return signature


def _phase_moles_map(row: Mapping[str, Any]) -> dict[str, float]:
    raw = row.get("phase_moles") or {}
    return {str(k): float(v) for k, v in raw.items() if v == v}


def _species_mol(row: Mapping[str, Any], species: str) -> float:
    mols = row.get("aq_molality_by_species") or {}
    m = mols.get(species)
    if m is None or m != m or m <= 0:
        return _MOL_FLOOR
    return float(m)


def phases_in_label(label: str) -> tuple[str, ...]:
    """Phase tokens in a category label (splits co-precip ``A + B``)."""
    if label in ("none", "aqueous", ""):
        return ()
    if " + " in label:
        return tuple(
            phase_from_label(p.strip()) for p in label.split(" + ") if p.strip()
        )
    return (phase_from_label(label),)


def solid_phase_set(
    cat: str,
    solid_phases: frozenset[str],
    collisions: frozenset[str],
) -> frozenset[str]:
    """Solid phase names encoded in a category label (empty if aqueous/none)."""
    if cat in ("none", "aqueous"):
        return frozenset()
    if " + " in cat:
        return frozenset(phases_in_label(cat))
    if label_is_solid(cat, solid_phases, collisions):
        return frozenset(phases_in_label(cat))
    return frozenset()


def mol_pair_scalar(row: Mapping[str, Any], cat_a: str, cat_b: str) -> float | None:
    """``sum(moles in A) - sum(moles in B)`` for solid–solid moles roots."""
    moles = _phase_moles_map(row)
    pa = phases_in_label(cat_a)
    pb = phases_in_label(cat_b)
    if not pa and not pb:
        return None
    if not any(p in moles for p in (*pa, *pb)):
        return None
    sa = sum(float(moles.get(p, 0.0)) for p in pa)
    sb = sum(float(moles.get(p, 0.0)) for p in pb)
    return sa - sb


def mol_solid_scalar(row: Mapping[str, Any], solid: str) -> float | None:
    """Precipitated moles of one solid (used to gate SI under EQUI pinning)."""
    moles = _phase_moles_map(row)
    phase = phase_from_label(solid)
    if phase not in moles:
        return 0.0
    return float(moles[phase])


def si_solid_scalar(row: Mapping[str, Any], solid: str) -> float | None:
    """Raw SI of one solid (phase name or label)."""
    si = row.get("si") or {}
    v = si.get(phase_from_label(solid))
    if v is None or v != v:
        return None
    return float(v)


def si_max_of_phases(
    row: Mapping[str, Any], phases: Sequence[str]
) -> float | None:
    """Max SI among ``phases`` (solid-set ↔ aqueous edge)."""
    sis: list[float] = []
    for p in phases:
        v = si_solid_scalar(row, p)
        if v is not None:
            sis.append(v)
    if not sis:
        return None
    return max(sis)


def mol_set_edge_scalar(
    row: Mapping[str, Any],
    set_a: frozenset[str],
    set_b: frozenset[str],
    *,
    mol_eps: float = DEFAULT_PRECIP_EPS,
) -> float | None:
    """Zero when the precipitated phase *set* switches (post-EQUI costability).

    - One phase in the symmetric difference → ``moles(phase) - mol_eps``
      (centered on presence threshold so brentq brackets nested solid↔join)
    - Disjoint exclusive singles → ``moles(A) - moles(B)``
    - Otherwise → signed sum of moles over ``only_a`` minus ``only_b``;
      when exactly one symdiff side is non-empty, subtract ``mol_eps`` with
      that side's sign
    """
    moles = _phase_moles_map(row)
    only_a = set_a - set_b
    only_b = set_b - set_a
    if len(only_a) + len(only_b) == 1:
        phase = next(iter(only_a or only_b))
        return float(moles.get(phase, 0.0)) - mol_eps
    if len(only_a) == 1 and len(only_b) == 1 and not (set_a & set_b):
        pa = next(iter(only_a))
        pb = next(iter(only_b))
        return float(moles.get(pa, 0.0)) - float(moles.get(pb, 0.0))
    if not only_a and not only_b:
        return None
    total = 0.0
    for p in only_a:
        total += float(moles.get(p, 0.0))
    for p in only_b:
        total -= float(moles.get(p, 0.0))
    # Center enter/leave of a multi-phase side on the presence threshold.
    if only_a and not only_b:
        total -= mol_eps
    elif only_b and not only_a:
        total += mol_eps
    return total


def solid_fluid_si_scalar(
    row: Mapping[str, Any],
    solid_cat: str,
    *,
    mol_eps: float = DEFAULT_PRECIP_EPS,
) -> float | None:
    """Solid↔fluid root via SI=0, moles-gated for EQUILIBRIUM_PHASES pinning.

    Under assemblage EQUI, a precipitated solid is forced to ``SI ≈ 0`` across its
    whole field, so raw SI often stays non-positive on *both* sides of an edge and
    ``brentq`` never brackets. Gate with precipitated moles of the named solid(s):

    - moles > eps → treat as the solid side (return ``max(SI, +ε)``)
    - moles ≤ eps → ungated SI (negative when undersaturated)
    """
    phases = phases_in_label(solid_cat)
    if not phases:
        return None
    moles = _phase_moles_map(row)
    present = any(float(moles.get(p, 0.0)) > mol_eps for p in phases)
    si = si_max_of_phases(row, phases)
    if si is None:
        return None
    if present:
        return max(si, 1e-6)
    return si


def aq_pair_scalar(row: Mapping[str, Any], cat_a: str, cat_b: str) -> float | None:
    """``log(m_A) - log(m_B)`` for aqueous–aqueous boundaries."""
    if cat_a in ("none", "aqueous") or cat_b in ("none", "aqueous"):
        return None
    return math.log(_species_mol(row, cat_a)) - math.log(_species_mol(row, cat_b))


def resolve_mineral_moles_pair_scalar(
    cat_a: str,
    cat_b: str,
    *,
    solid_phases: frozenset[str] = frozenset(),
    collisions: frozenset[str] = frozenset(),
) -> tuple[Callable[[Mapping[str, Any]], float | None] | None, str]:
    """Roots for moles-predominance fills (assemblage EQUI).

    - ``mol``: solid–solid precipitated-mole difference
    - ``aq_solid``: solid–fluid moles-gated SI=0
    - ``aq`` / ``conv``: aqueous ratio / convergence
    """
    if cat_a == "none" or cat_b == "none":
        return (lambda row: 1.0 if row.get("converged") else -1.0), "conv"

    a_solid = label_is_solid(cat_a, solid_phases, collisions)
    b_solid = label_is_solid(cat_b, solid_phases, collisions)

    if a_solid and b_solid:
        return (lambda row: mol_pair_scalar(row, cat_a, cat_b)), "mol"
    if a_solid and not b_solid:
        return (lambda row: solid_fluid_si_scalar(row, cat_a)), "aq_solid"
    if b_solid and not a_solid:
        return (lambda row: solid_fluid_si_scalar(row, cat_b)), "aq_solid"
    if cat_a == "aqueous" or cat_b == "aqueous":
        return None, ""
    return (lambda row: aq_pair_scalar(row, cat_a, cat_b)), "aq"


def resolve_mineral_costability_pair_scalar(
    cat_a: str,
    cat_b: str,
    *,
    solid_phases: frozenset[str] = frozenset(),
    collisions: frozenset[str] = frozenset(),
) -> tuple[Callable[[Mapping[str, Any]], float | None] | None, str]:
    """Roots for post-precip co-stability fills (assemblage EQUI).

    - ``mol`` / ``mol_set``: precipitated-mole set edges (phase enter/leave)
    - ``aq_solid``: solid-set ↔ fluid via moles-gated SI
    - ``aq`` / ``conv``: aqueous ratio / convergence
    """
    if cat_a == "none" or cat_b == "none":
        return (lambda row: 1.0 if row.get("converged") else -1.0), "conv"

    set_a = solid_phase_set(cat_a, solid_phases, collisions)
    set_b = solid_phase_set(cat_b, solid_phases, collisions)
    a_solid = bool(set_a)
    b_solid = bool(set_b)

    if a_solid and not b_solid:
        return (lambda row: solid_fluid_si_scalar(row, cat_a)), "aq_solid"
    if b_solid and not a_solid:
        return (lambda row: solid_fluid_si_scalar(row, cat_b)), "aq_solid"
    if a_solid and b_solid:
        only_a = set_a - set_b
        only_b = set_b - set_a
        if (
            len(only_a) == 1
            and len(only_b) == 1
            and not (set_a & set_b)
        ):
            return (lambda row: mol_set_edge_scalar(row, set_a, set_b)), "mol"
        return (lambda row: mol_set_edge_scalar(row, set_a, set_b)), "mol_set"
    if cat_a == "aqueous" or cat_b == "aqueous":
        return None, ""
    return (lambda row: aq_pair_scalar(row, cat_a, cat_b)), "aq"


def resolve_mineral_pair_scalar(
    cat_a: str,
    cat_b: str,
    *,
    solid_phases: frozenset[str] = frozenset(),
    collisions: frozenset[str] = frozenset(),
    category_mode: MineralCategoryMode = "moles",
) -> tuple[Callable[[Mapping[str, Any]], float | None] | None, str]:
    """Dispatch pair roots by mineral category mode (default: moles)."""
    if _normalize_category_mode(category_mode) == "costability":
        return resolve_mineral_costability_pair_scalar(
            cat_a,
            cat_b,
            solid_phases=solid_phases,
            collisions=collisions,
        )
    return resolve_mineral_moles_pair_scalar(
        cat_a,
        cat_b,
        solid_phases=solid_phases,
        collisions=collisions,
    )
