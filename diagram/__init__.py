"""Phase diagram result packing.

Submodules are imported lazily so ``from diagram.packer import …`` (used by
spawned ProcessPool workers) does not pull ``vectors`` → ``gas_limits`` →
``boundary_trace`` and create a circular import under ``multiprocessing`` spawn.
"""
from __future__ import annotations

from typing import Any

__all__ = [
    "pack_grid_results",
    "pack_mineral_grid_results",
    "pack_traced_display",
    "pack_traced_mineral_display",
    "resolve_phase_names",
    "system_elements_from_totals",
]


def __getattr__(name: str) -> Any:
    if name in ("pack_grid_results", "pack_mineral_grid_results"):
        from .packer import pack_grid_results, pack_mineral_grid_results

        return {
            "pack_grid_results": pack_grid_results,
            "pack_mineral_grid_results": pack_mineral_grid_results,
        }[name]
    if name in ("pack_traced_display", "pack_traced_mineral_display"):
        from .vectors import pack_traced_display, pack_traced_mineral_display

        return {
            "pack_traced_display": pack_traced_display,
            "pack_traced_mineral_display": pack_traced_mineral_display,
        }[name]
    if name in ("resolve_phase_names", "system_elements_from_totals"):
        from .phases import resolve_phase_names, system_elements_from_totals

        return {
            "resolve_phase_names": resolve_phase_names,
            "system_elements_from_totals": system_elements_from_totals,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
