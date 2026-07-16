"""Application services (compute jobs, species helpers).

Imports are lazy so ``phreeqc.sweep`` can pull ``job_control`` without
loading ``compute`` → ``diagram.vectors`` → ``phreeqc.adaptive`` (cycle).
"""

__all__ = [
    "create_job",
    "get_job",
    "get_job_result",
    "run_compute_job",
    "species_suggestions",
]


def __getattr__(name: str):
    if name in {"create_job", "get_job", "get_job_result", "run_compute_job"}:
        from . import compute

        return getattr(compute, name)
    if name == "species_suggestions":
        from .species import species_suggestions

        return species_suggestions
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
