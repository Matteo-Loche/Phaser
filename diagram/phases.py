"""Phase resolution helpers for diagram computation."""
from __future__ import annotations

from .. import config
from ..db.catalog_store import list_phases, require_ready
from ..db.registry import DatabaseRecord
from ..phreeqc.catalog import is_gas
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
    rec: DatabaseRecord,
    *,
    phases: list[str] | None,
    system_elems: set[str],
) -> tuple[str, ...]:
    db_key = require_ready(rec)
    if phases:
        names = [p for p in phases if not is_gas(p)]
    else:
        catalog_phases = list_phases(
            db_key,
            system_elements=system_elems,
            exclude_gases=True,
        )
        names = [p["name"] for p in catalog_phases]
    return tuple(names[: config.MAX_PHASES_PER_JOB])
