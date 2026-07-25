"""Inert background electrolyte (Bgc+/Bga-) for electroneutral grid inputs."""
from __future__ import annotations

from typing import Callable, Mapping

from ..chemistry.charges import formal_eq_of_total_key

BG_CATION = "Bgc"
BG_ANION = "Bga"
BG_CATION_SPECIES = "Bgc+"
BG_ANION_SPECIES = "Bga-"
BG_TITRANT_BASE = "BgcOH"

EXCLUDED_ELEMENTS = frozenset({BG_CATION, BG_ANION})
EXCLUDED_SPECIES = frozenset({BG_CATION_SPECIES, BG_ANION_SPECIES, BG_TITRANT_BASE})

# Run once per IPhreeqc instance after load_database().
WORKER_DEFINITIONS = """SOLUTION_MASTER_SPECIES
    Bgc    Bgc+   0.0   Bgc   100.0
    Bga    Bga-   0.0   Bga   100.0
SOLUTION_SPECIES
    Bgc+ = Bgc+
        log_k 0
        -gamma 4.0 0.075
    Bga- = Bga-
        log_k 0
        -gamma 3.5 0.015
PHASES
Fix_H+
    H+ = H+
    log_k 0
BgcOH
    BgcOH = Bgc+ + OH-
    log_k 0
END
"""


def estimate_net_eq(
    ph: float,
    totals: Mapping[str, float],
    eq_of: Callable[[str], float] | None = None,
) -> float:
    """First-order net charge (eq/kgw) at ``ph`` with ``totals``."""
    eq_fn = eq_of or formal_eq_of_total_key
    net = 10.0 ** (-ph) - 10.0 ** (ph - 14.0)
    for element, molality in totals.items():
        net += eq_fn(element) * float(molality)
    return net


def charge_side(
    ph: float,
    totals: Mapping[str, float],
    *,
    flip: bool = False,
    eq_of: Callable[[str], float] | None = None,
) -> tuple[str, float]:
    """(element_carrying_charge, starting_estimate_molality)."""
    net = estimate_net_eq(ph, totals, eq_of)
    side = BG_ANION if net > 0 else BG_CATION
    if flip:
        side = BG_CATION if side == BG_ANION else BG_ANION
    return side, max(abs(net), 1e-8)


def medium_lines(
    ph: float,
    totals: Mapping[str, float],
    *,
    molality: float = 0.0,
    flip: bool = False,
    eq_of: Callable[[str], float] | None = None,
    indent: str = "    ",
) -> str:
    """SOLUTION lines for the background medium."""
    side, start = charge_side(ph, totals, flip=flip, eq_of=eq_of)
    other = BG_CATION if side == BG_ANION else BG_ANION
    if molality <= 0.0:
        return f"{indent}{side} {start:.6g} charge"
    return (
        f"{indent}{other} {molality:.6g}\n"
        f"{indent}{side} {max(molality, start):.6g} charge"
    )


def recommended_medium_molality(
    totals: Mapping[str, float],
    *,
    safety: float = 3.0,
    eq_of: Callable[[str], float] | None = None,
) -> float:
    """Lower bound on fixed medium for future assemblage mode."""
    eq_fn = eq_of or formal_eq_of_total_key
    demand = sum((abs(eq_fn(el)) + 2.0) * float(m) for el, m in totals.items())
    return safety * max(demand, 1e-6)
