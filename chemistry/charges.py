"""Formal charge estimates for dummy-medium charge-balance guessing."""
from __future__ import annotations

import re

_REDOX_SUFFIX = re.compile(r"\((-?\d+)\)$")
_ELEMENT_SYMBOL = re.compile(r"^([A-Z][a-z]?)")

# Bare-element first guess when no PHREEQC redox suffix (e.g. Fe, not Fe(3)).
# Redox totals like C(4) / S(6) always use the parenthetical valence.
# Elements absent from this map default to 0; flip-retry in evaluate_point()
# corrects wrong charge-side guesses regardless of database coverage.
_DEFAULT_FORMAL_EQ: dict[str, float] = {
    "Na": 1, "K": 1, "Li": 1, "Ag": 1, "Cs": 1, "Rb": 1,
    "Ca": 2, "Mg": 2, "Sr": 2, "Ba": 2, "Fe": 2, "Mn": 2, "Zn": 2,
    "Cu": 2, "Pb": 2, "Cd": 2, "Ni": 2, "Co": 2,
    "Al": 3, "Cr": 3, "La": 3,
    "Cl": -1, "Br": -1, "F": -1, "I": -1,
    "S": -2, "Se": -2,
    "N": -1,
    "P": -2, "As": -1,
    "C": 0, "Si": 0, "B": 0,
}


def formal_eq_of_total_key(key: str) -> float:
    """Formal equivalents per mole of a PHREEQC total key (first charge guess).

    Positive net charge => an anion (Bga-) should balance; negative => cation (Bgc+).
    Unknown bare elements return 0; wrong guesses are corrected by flip-retry.
    """
    text = (key or "").strip()
    m = _REDOX_SUFFIX.search(text)
    if m:
        return float(m.group(1))
    m = _ELEMENT_SYMBOL.match(text)
    symbol = m.group(1) if m else text
    return _DEFAULT_FORMAL_EQ.get(symbol, 0.0)
