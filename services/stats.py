"""Record and query per-server compute usage statistics."""
from __future__ import annotations

from typing import Any

from ..api.models import ComputeRequest
from ..db import stats_store
from ..db.registry import DatabaseRecord
from ..diagram.packer import effective_layer_elements
from ..diagram.phases import system_elements_from_totals


def init_stats() -> None:
    stats_store.init_schema()


def record_compute(
    body: ComputeRequest,
    *,
    db_rec: DatabaseRecord,
    compute_ms: float | None,
    n_phreeqc_runs: int | None,
    queue_position_at_start: int | None,
    queue_wait_ms: float | None,
    mode_id: str = "phase-diagram",
) -> None:
    sys_tuple = system_elements_from_totals(body.totals, body.system_elements)
    try:
        stats_store.record_compute_event(
            mode_id=mode_id,
            db_id=db_rec.id,
            grid_levels=body.ph_levels,
            layer_solids=body.layer_solids,
            layer_aqueous=body.layer_aqueous,
            layer_elements=effective_layer_elements(sys_tuple, body.layer_elements),
            adaptive=body.adaptive_boundaries,
            system_elements=sys_tuple,
            n_phreeqc_runs=n_phreeqc_runs,
            compute_ms=compute_ms,
            queue_position_at_start=queue_position_at_start,
            queue_wait_ms=queue_wait_ms,
        )
    except Exception:
        # Stats must never break a successful compute.
        pass


def get_summary(window: str | None = None) -> dict[str, Any]:
    return stats_store.get_summary(window=window)
