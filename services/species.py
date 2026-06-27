"""Species picker suggestions from database elements and known totals."""
from __future__ import annotations

from .. import config


def species_suggestions(db_elements: list[str]) -> list[str]:
    """Master-species labels for the 'add species' picker (redox totals + elements)."""
    ordered: list[str] = []
    for name in config.KNOWN_TOTALS:
        if name not in ordered:
            ordered.append(name)
    for elem in db_elements:
        if elem not in ordered:
            ordered.append(elem)
    return ordered
