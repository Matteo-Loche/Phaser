"""Species picker suggestions from database elements and known totals."""
from __future__ import annotations

from .. import config
from ..phreeqc.engine import element_from_total_key


def species_for_database(db_elements: list[str]) -> list[str]:
    """Bare element symbols for the chemical-system picker (e.g. ``C``, not ``C(4)``).

    Preference order follows ``KNOWN_TOTALS`` (mapped to parent elements), then any
    remaining database elements alphabetically as returned by the catalog.
    """
    db_set = set(db_elements)
    ordered: list[str] = []
    for name in config.KNOWN_TOTALS:
        elem = element_from_total_key(name)
        if elem in db_set and elem not in ordered:
            ordered.append(elem)
    for elem in db_elements:
        if elem not in ordered:
            ordered.append(elem)
    return ordered


def species_suggestions(db_elements: list[str]) -> list[str]:
    """Master-species labels for the 'add species' picker (general elements only)."""
    return species_for_database(db_elements)
