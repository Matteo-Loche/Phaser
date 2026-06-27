"""Phase resolution helpers for diagram computation."""
from __future__ import annotations

from .. import config
from ..db.parser import filter_phases, is_gas, load_phase_catalog
from ..phreeqc.engine import element_from_total_key


def system_elements_from_totals(
    totals: dict[str, float],
    explicit: list[str] | None,
) -> tuple[str, ...]:
    if explicit:
        return tuple(sorted(set(explicit)))
    elems = {element_from_total_key(k) for k in totals.keys()}
    return tuple(sorted(elems))


def resolve_phase_names(
    db_path: str,
    *,
    phases: list[str] | None,
    system_elems: set[str],
) -> tuple[str, ...]:
    if phases:
        names = [p for p in phases if not is_gas(p)]
    else:
        names = [
            p.name
            for p in filter_phases(
                db_path,
                system_elements=system_elems,
                exclude_gases=True,
            )
        ]
    return tuple(names[: config.MAX_PHASES_PER_JOB])


def phase_element_map(db_path: str) -> dict[str, frozenset[str]]:
    return {p.name: p.elements for p in load_phase_catalog(db_path)}
