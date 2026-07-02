"""SQLite-backed per-server compute usage statistics."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import config

_LOCK = threading.RLock()
_SCHEMA_VERSION = 1


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    path = Path(config.STATS_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema() -> None:
    with _LOCK:
        conn = _connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS compute_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    finished_at TEXT NOT NULL,
                    mode_id TEXT NOT NULL DEFAULT 'phase-diagram',
                    db_id TEXT,
                    grid_levels INTEGER NOT NULL,
                    layer_solids INTEGER NOT NULL,
                    layer_aqueous INTEGER NOT NULL,
                    layer_elements INTEGER NOT NULL,
                    adaptive INTEGER NOT NULL,
                    n_elements INTEGER NOT NULL,
                    n_phreeqc_runs INTEGER,
                    compute_ms REAL,
                    queue_position_at_start INTEGER,
                    queue_wait_ms REAL,
                    system_elements TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_compute_events_finished
                    ON compute_events(finished_at);
                CREATE INDEX IF NOT EXISTS idx_compute_events_db
                    ON compute_events(db_id);
                """
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(_SCHEMA_VERSION),),
            )
            conn.commit()
        finally:
            conn.close()


def layer_config_label(
    *,
    layer_solids: bool,
    layer_aqueous: bool,
    layer_elements: bool,
) -> str:
    parts: list[str] = []
    if layer_solids:
        parts.append("Solid")
    if layer_aqueous:
        parts.append("Aqueous")
    label = " + ".join(parts) if parts else "None"
    if layer_elements:
        label += " + subsets"
    return label


def record_compute_event(
    *,
    mode_id: str,
    db_id: str | None,
    grid_levels: int,
    layer_solids: bool,
    layer_aqueous: bool,
    layer_elements: bool,
    adaptive: bool,
    system_elements: tuple[str, ...] | list[str],
    n_phreeqc_runs: int | None,
    compute_ms: float | None,
    queue_position_at_start: int | None,
    queue_wait_ms: float | None,
) -> None:
    elems = sorted(set(system_elements))
    finished_at = _utcnow()
    with _LOCK:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO compute_events (
                    finished_at, mode_id, db_id, grid_levels,
                    layer_solids, layer_aqueous, layer_elements, adaptive,
                    n_elements, n_phreeqc_runs, compute_ms,
                    queue_position_at_start, queue_wait_ms, system_elements
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    finished_at,
                    mode_id,
                    db_id,
                    grid_levels,
                    int(layer_solids),
                    int(layer_aqueous),
                    int(layer_elements),
                    int(adaptive),
                    len(elems),
                    n_phreeqc_runs,
                    compute_ms,
                    queue_position_at_start,
                    queue_wait_ms,
                    json.dumps(elems),
                ),
            )
            conn.commit()
        finally:
            conn.close()


def _format_chemical_system(system_elements_json: str) -> str:
    try:
        elems = json.loads(system_elements_json)
    except (json.JSONDecodeError, TypeError):
        return system_elements_json
    if not isinstance(elems, list):
        return system_elements_json
    return " · ".join(str(e) for e in elems)


def _top_chemical_systems(conn: sqlite3.Connection, limit: int = 12) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT system_elements, COUNT(*) AS count
        FROM compute_events
        WHERE mode_id = 'phase-diagram'
        GROUP BY system_elements
        ORDER BY count DESC, system_elements ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        raw = row["system_elements"]
        try:
            elements = json.loads(raw)
            if not isinstance(elements, list):
                elements = []
        except (json.JSONDecodeError, TypeError):
            elements = []
        out.append(
            {
                "system": _format_chemical_system(raw),
                "elements": elements,
                "count": int(row["count"]),
            }
        )
    return out


def _top_rows(
    conn: sqlite3.Connection,
    sql: str,
    limit: int = 8,
) -> list[dict[str, Any]]:
    rows = conn.execute(sql, (limit,)).fetchall()
    return [dict(r) for r in rows]


_ACTIVITY_BUCKET_MINUTES = 5


def _activity_last_24h(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Sub-hour compute activity for the trailing 24 hours (oldest first).

    Buckets are ``_ACTIVITY_BUCKET_MINUTES`` wide (96 points at 15 min) so the
    UI can draw a fine-resolution time series. Each entry is
    ``{"bucket_start": ISO-UTC, "count": int, "avg_wait_ms": float | None}``.
    """
    from datetime import timedelta

    step = timedelta(minutes=_ACTIVITY_BUCKET_MINUTES)
    n_buckets = (24 * 60) // _ACTIVITY_BUCKET_MINUTES

    now = datetime.now(timezone.utc)
    floored_minute = (now.minute // _ACTIVITY_BUCKET_MINUTES) * _ACTIVITY_BUCKET_MINUTES
    current = now.replace(minute=floored_minute, second=0, microsecond=0)
    buckets: list[datetime] = [current - step * i for i in range(n_buckets - 1, -1, -1)]

    counts = {b.isoformat(): 0 for b in buckets}
    wait_sum = {b.isoformat(): 0.0 for b in buckets}
    wait_n = {b.isoformat(): 0 for b in buckets}

    window_start = buckets[0]
    rows = conn.execute(
        """
        SELECT finished_at, queue_wait_ms FROM compute_events
        WHERE mode_id = 'phase-diagram' AND finished_at >= ?
        """,
        (window_start.isoformat(),),
    ).fetchall()

    for row in rows:
        ts = row["finished_at"]
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        m = (dt.minute // _ACTIVITY_BUCKET_MINUTES) * _ACTIVITY_BUCKET_MINUTES
        key = dt.replace(minute=m, second=0, microsecond=0).isoformat()
        if key in counts:
            counts[key] += 1
            wait = row["queue_wait_ms"]
            if wait is not None:
                wait_sum[key] += wait
                wait_n[key] += 1

    out: list[dict[str, Any]] = []
    for b in buckets:
        k = b.isoformat()
        out.append(
            {
                "bucket_start": k,
                "count": counts[k],
                "avg_wait_ms": (wait_sum[k] / wait_n[k]) if wait_n[k] else None,
            }
        )
    return out


def get_summary() -> dict[str, Any]:
    with _LOCK:
        conn = _connect()
        try:
            agg = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_diagrams,
                    MIN(finished_at) AS first_compute_at,
                    MAX(finished_at) AS last_compute_at,
                    AVG(compute_ms) AS avg_compute_ms,
                    AVG(queue_position_at_start) AS avg_queue_position,
                    AVG(queue_wait_ms) AS avg_queue_wait_ms,
                    SUM(CASE WHEN adaptive = 1 THEN 1 ELSE 0 END) AS adaptive_count,
                    SUM(CASE WHEN adaptive = 0 THEN 1 ELSE 0 END) AS uniform_count
                FROM compute_events
                WHERE mode_id = 'phase-diagram'
                """
            ).fetchone()

            total = int(agg["total_diagrams"] or 0)
            if total == 0:
                return {
                    "total_diagrams": 0,
                    "first_compute_at": None,
                    "last_compute_at": None,
                    "avg_compute_ms": None,
                    "avg_queue_position": None,
                    "avg_queue_wait_ms": None,
                    "adaptive_vs_uniform": {"adaptive": 0, "uniform": 0},
                    "activity_24h": _activity_last_24h(conn),
                    "top_databases": [],
                    "top_grid_sizes": [],
                    "top_layer_configs": [],
                    "top_chemical_systems": [],
                }

            top_databases = _top_rows(
                conn,
                """
                SELECT db_id, COUNT(*) AS count
                FROM compute_events
                WHERE mode_id = 'phase-diagram' AND db_id IS NOT NULL AND db_id != ''
                GROUP BY db_id
                ORDER BY count DESC, db_id ASC
                LIMIT ?
                """,
            )
            top_grid_sizes = _top_rows(
                conn,
                """
                SELECT grid_levels, COUNT(*) AS count
                FROM compute_events
                WHERE mode_id = 'phase-diagram'
                GROUP BY grid_levels
                ORDER BY count DESC, grid_levels DESC
                LIMIT ?
                """,
            )

            layer_rows = conn.execute(
                """
                SELECT layer_solids, layer_aqueous, layer_elements, COUNT(*) AS count
                FROM compute_events
                WHERE mode_id = 'phase-diagram'
                GROUP BY layer_solids, layer_aqueous, layer_elements
                ORDER BY count DESC
                LIMIT ?
                """,
                (8,),
            ).fetchall()
            top_layer_configs = [
                {
                    "label": layer_config_label(
                        layer_solids=bool(r["layer_solids"]),
                        layer_aqueous=bool(r["layer_aqueous"]),
                        layer_elements=bool(r["layer_elements"]),
                    ),
                    "count": int(r["count"]),
                }
                for r in layer_rows
            ]

            top_chemical_systems = _top_chemical_systems(conn)

            return {
                "total_diagrams": total,
                "first_compute_at": agg["first_compute_at"],
                "last_compute_at": agg["last_compute_at"],
                "avg_compute_ms": agg["avg_compute_ms"],
                "avg_queue_position": agg["avg_queue_position"],
                "avg_queue_wait_ms": agg["avg_queue_wait_ms"],
                "adaptive_vs_uniform": {
                    "adaptive": int(agg["adaptive_count"] or 0),
                    "uniform": int(agg["uniform_count"] or 0),
                },
                "activity_24h": _activity_last_24h(conn),
                "top_databases": top_databases,
                "top_grid_sizes": top_grid_sizes,
                "top_layer_configs": top_layer_configs,
                "top_chemical_systems": top_chemical_systems,
            }
        finally:
            conn.close()
