"""Species picker suggestions from database elements and known totals."""
from __future__ import annotations

from .. import config
from ..phreeqc.engine import element_from_total_key


def species_for_database(db_elements: list[str]) -> list[str]:
    """Master-species labels valid for a given database (element must be present)."""
    db_set = set(db_elements)
    ordered: list[str] = []
    for name in config.KNOWN_TOTALS:
        if element_from_total_key(name) in db_set and name not in ordered:
            ordered.append(name)
    for elem in db_elements:
        if elem not in ordered:
            ordered.append(elem)
    return ordered


def species_suggestions(db_elements: list[str]) -> list[str]:
    """Master-species labels for the 'add species' picker (redox totals + elements)."""
    return species_for_database(db_elements)
