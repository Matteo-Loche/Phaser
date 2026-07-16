"""SQLite-backed per-server compute usage statistics."""
from __future__ import annotations

import json
import math
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .. import config

_LOCK = threading.RLock()
_SCHEMA_VERSION = 1

# Cap ranked lists so the dashboard stays scannable as usage grows.
TOP_LIMIT = 15

MODE_LABELS = {
    "phase-diagram": "Predominance",
    "mineral-stability": "Mineral Stability",
}

# Trailing windows for GET /api/stats?window=
# Fixed windows use explicit bucket widths (~250–400 points).
# ``all`` resolves span + bucket from the earliest event at query time.
STATS_WINDOWS: dict[str, dict[str, int | None]] = {
    "24h": {"hours": 24, "bucket_minutes": 5},
    "7d": {"hours": 24 * 7, "bucket_minutes": 30},
    "30d": {"hours": 24 * 30, "bucket_minutes": 120},  # 2 h
    "90d": {"hours": 24 * 90, "bucket_minutes": 360},  # 6 h
    "1y": {"hours": 24 * 365, "bucket_minutes": 720},  # 12 h
    "all": {"hours": None, "bucket_minutes": None},
}
DEFAULT_STATS_WINDOW = "30d"

_MODE_FILTER = "mode_id IN ('phase-diagram', 'mineral-stability')"


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


def normalize_stats_window(window: str | None) -> str:
    key = str(window or DEFAULT_STATS_WINDOW).strip().lower()
    if key in STATS_WINDOWS:
        return key
    return DEFAULT_STATS_WINDOW


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


def _bucket_minutes_for_span_hours(hours: float) -> int:
    """Pick a bucket width that keeps roughly 250–400 activity points."""
    h = max(1.0, float(hours))
    candidates = (5, 15, 30, 60, 120, 180, 360, 720, 1440)
    target = 320.0
    best = 1440
    best_score = float("inf")
    for minutes in candidates:
        n = (h * 60.0) / minutes
        if n < 60:
            continue
        if n > 480:
            continue
        score = abs(n - target)
        if score < best_score:
            best_score = score
            best = minutes
    if (h * 60.0) / best > 480:
        return 1440
    return best


def _parse_iso_utc(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _resolve_window(
    conn: sqlite3.Connection,
    window: str | None,
) -> tuple[str, datetime, int, int]:
    """Return ``(window_id, window_start, hours, bucket_minutes)``."""
    key = normalize_stats_window(window)
    now = datetime.now(timezone.utc)
    spec = STATS_WINDOWS[key]

    if key == "all":
        row = conn.execute(
            f"""
            SELECT MIN(finished_at) AS first_at
            FROM compute_events
            WHERE {_MODE_FILTER}
            """
        ).fetchone()
        first = _parse_iso_utc(row["first_at"] if row else None)
        if first is None:
            return key, now - timedelta(hours=24), 24, 5
        # Pad slightly so the first event lands inside the first bucket.
        start = first - timedelta(minutes=1)
        hours = max(1, int(math.ceil((now - start).total_seconds() / 3600.0)))
        bucket = _bucket_minutes_for_span_hours(hours)
        return key, start, hours, bucket

    hours = int(spec["hours"] or 24)
    bucket = int(spec["bucket_minutes"] or 60)
    start = now - timedelta(hours=hours)
    return key, start, hours, bucket


def _top_chemical_systems(
    conn: sqlite3.Connection,
    *,
    since: str,
    limit: int = TOP_LIMIT,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT system_elements, COUNT(*) AS count
        FROM compute_events
        WHERE {_MODE_FILTER} AND finished_at >= ?
        GROUP BY system_elements
        ORDER BY count DESC, system_elements ASC
        LIMIT ?
        """,
        (since, limit),
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
    params: tuple[Any, ...],
) -> list[dict[str, Any]]:
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def _top_modes(
    conn: sqlite3.Connection,
    *,
    since: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT mode_id, COUNT(*) AS count
        FROM compute_events
        WHERE {_MODE_FILTER} AND finished_at >= ?
        GROUP BY mode_id
        ORDER BY count DESC, mode_id ASC
        """,
        (since,),
    ).fetchall()
    by_id = {str(r["mode_id"]): int(r["count"]) for r in rows}
    # Always emit both known modes so the UI can show a stable pair.
    out: list[dict[str, Any]] = []
    for mode_id, label in MODE_LABELS.items():
        out.append(
            {
                "mode_id": mode_id,
                "label": label,
                "count": by_id.get(mode_id, 0),
            }
        )
    out.sort(key=lambda r: (-r["count"], r["label"]))
    return out


def _activity_series(
    conn: sqlite3.Connection,
    *,
    hours: int,
    bucket_minutes: int,
) -> list[dict[str, Any]]:
    """Bucketed compute activity for the trailing ``hours`` (oldest first)."""
    step = timedelta(minutes=bucket_minutes)
    n_buckets = max(1, (hours * 60) // bucket_minutes)

    now = datetime.now(timezone.utc)
    floored_minute = (now.minute // bucket_minutes) * bucket_minutes
    current = now.replace(minute=floored_minute, second=0, microsecond=0)
    buckets: list[datetime] = [
        current - step * i for i in range(n_buckets - 1, -1, -1)
    ]

    counts = {b.isoformat(): 0 for b in buckets}
    wait_sum = {b.isoformat(): 0.0 for b in buckets}
    wait_n = {b.isoformat(): 0 for b in buckets}

    window_start = buckets[0]
    rows = conn.execute(
        f"""
        SELECT finished_at, queue_wait_ms FROM compute_events
        WHERE {_MODE_FILTER} AND finished_at >= ?
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
        m = (dt.minute // bucket_minutes) * bucket_minutes
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


def get_summary(window: str | None = None) -> dict[str, Any]:
    with _LOCK:
        conn = _connect()
        try:
            window_id, window_start, hours, bucket_minutes = _resolve_window(
                conn, window
            )
            since = window_start.isoformat()
            activity = _activity_series(
                conn,
                hours=hours,
                bucket_minutes=bucket_minutes,
            )
            empty = {
                "window": window_id,
                "window_hours": hours,
                "bucket_minutes": bucket_minutes,
                "total_diagrams": 0,
                "first_compute_at": None,
                "last_compute_at": None,
                "avg_compute_ms": None,
                "avg_queue_position": None,
                "avg_queue_wait_ms": None,
                "adaptive_vs_uniform": {"adaptive": 0, "uniform": 0},
                "top_modes": _top_modes(conn, since=since),
                "activity": activity,
                # Back-compat alias for older clients / docs.
                "activity_24h": activity,
                "top_databases": [],
                "top_grid_sizes": [],
                "top_layer_configs": [],
                "top_chemical_systems": [],
            }

            agg = conn.execute(
                f"""
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
                WHERE {_MODE_FILTER} AND finished_at >= ?
                """,
                (since,),
            ).fetchone()

            total = int(agg["total_diagrams"] or 0)
            top_modes = _top_modes(conn, since=since)
            if total == 0:
                empty["top_modes"] = top_modes
                return empty

            top_databases = _top_rows(
                conn,
                f"""
                SELECT db_id, COUNT(*) AS count
                FROM compute_events
                WHERE {_MODE_FILTER} AND finished_at >= ?
                  AND db_id IS NOT NULL AND db_id != ''
                GROUP BY db_id
                ORDER BY count DESC, db_id ASC
                LIMIT ?
                """,
                (since, TOP_LIMIT),
            )
            top_grid_sizes = _top_rows(
                conn,
                f"""
                SELECT grid_levels, COUNT(*) AS count
                FROM compute_events
                WHERE {_MODE_FILTER} AND finished_at >= ?
                GROUP BY grid_levels
                ORDER BY count DESC, grid_levels DESC
                LIMIT ?
                """,
                (since, TOP_LIMIT),
            )

            layer_rows = conn.execute(
                f"""
                SELECT layer_solids, layer_aqueous, layer_elements, COUNT(*) AS count
                FROM compute_events
                WHERE {_MODE_FILTER} AND finished_at >= ?
                GROUP BY layer_solids, layer_aqueous, layer_elements
                ORDER BY count DESC
                LIMIT ?
                """,
                (since, TOP_LIMIT),
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

            return {
                "window": window_id,
                "window_hours": hours,
                "bucket_minutes": bucket_minutes,
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
                "top_modes": top_modes,
                "activity": activity,
                "activity_24h": activity,
                "top_databases": top_databases,
                "top_grid_sizes": top_grid_sizes,
                "top_layer_configs": top_layer_configs,
                "top_chemical_systems": _top_chemical_systems(conn, since=since),
            }
        finally:
            conn.close()
