"""KNOBS retry ladder: escalating numerical settings for hard grid points."""
from __future__ import annotations

from collections import Counter
from typing import Sequence

KNOBS_PROFILES: dict[str, str] = {
    "default": (
        "KNOBS\n"
        "    -iterations            100\n"
        "    -convergence_tolerance 1e-8\n"
        "    -step_size             100\n"
        "    -pe_step_size          10\n"
        "    -diagonal_scale        false\n"
        "    -numerical_derivatives false\n"
        "END\n"
    ),
    "damped": (
        "KNOBS\n"
        "    -iterations            400\n"
        "    -convergence_tolerance 1e-8\n"
        "    -step_size             10\n"
        "    -pe_step_size          5\n"
        "    -diagonal_scale        true\n"
        "    -numerical_derivatives false\n"
        "END\n"
    ),
    "robust": (
        "KNOBS\n"
        "    -iterations            1200\n"
        "    -convergence_tolerance 1e-8\n"
        "    -step_size             2\n"
        "    -pe_step_size          2\n"
        "    -diagonal_scale        true\n"
        "    -numerical_derivatives true\n"
        "END\n"
    ),
}

KNOBS_LADDER_DEFAULT: tuple[str, ...] = ("default", "damped", "robust")
KNOBS_PROFILE_INDEX: dict[str, int] = {name: i for i, name in enumerate(KNOBS_LADDER_DEFAULT)}

knobs_stats: Counter = Counter()


def run_single_profile(phreeqc, profile: str, body: str, run_once=None):
    """Run ``body`` under one explicit KNOBS profile (prefix always applied)."""
    if run_once is None:
        from .engine import _run_phreeqc_string

        run_once = _run_phreeqc_string
    return run_once(phreeqc, KNOBS_PROFILES[profile] + body)


def run_with_knobs(
    phreeqc,
    body: str,
    ladder: Sequence[str] = KNOBS_LADDER_DEFAULT,
    run_once=None,
):
    """Run ``body`` under escalating KNOBS profiles; returns (selected, rung_index)."""
    if run_once is None:
        from .engine import _run_phreeqc_string

        run_once = _run_phreeqc_string
    last_exc: Exception | None = None
    for rung, name in enumerate(ladder):
        try:
            selected = run_single_profile(phreeqc, name, body, run_once=run_once)
        except Exception as exc:
            last_exc = exc
            continue
        knobs_stats[name] += 1
        return selected, rung
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("empty KNOBS ladder")
