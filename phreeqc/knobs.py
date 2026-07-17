"""KNOBS retry ladder: escalating numerical settings for hard grid points."""
from __future__ import annotations

from .. import config

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


def ladder_for_mode(mode: str | None) -> tuple[str, ...]:
    """Profiles to try for a knobs_mode (flip-retry is handled in evaluate_point)."""
    m = config.normalize_knobs_mode(mode)
    if m == "default":
        return ("default",)
    if m == "standard":
        return ("default", "damped")
    return KNOBS_LADDER_DEFAULT


def run_single_profile(phreeqc, profile: str, body: str, run_once=None):
    """Run ``body`` under one explicit KNOBS profile (prefix always applied)."""
    if run_once is None:
        from .engine import _run_phreeqc_string

        run_once = _run_phreeqc_string
    return run_once(phreeqc, KNOBS_PROFILES[profile] + body)
