"""SQLite-backed PHREEQC catalog cache."""
from __future__ import annotations

import hashlib
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import config
from ..phreeqc.catalog import (
    SCHEMA_VERSION,
    DatabaseCatalogSnapshot,
    element_from_total_key,
    element_symbols_from_totals,
    is_gas,
    subset_key,
    subsets_for_scan,
)
from .registry import DatabaseRecord

_LOCK = threading.RLock()


@dataclass(frozen=True)
class CatalogStatus:
    db_key: str
    status: str
    error: str | None
    scanned_at: str | None
    schema_version: int


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    path = Path(config.CATALOG_DB)
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
                CREATE TABLE IF NOT EXISTS databases (
                    db_key TEXT PRIMARY KEY,
                    db_id TEXT,
                    path TEXT NOT NULL,
                    filename TEXT,
                    source TEXT,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    scanned_at TEXT,
                    schema_version INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS totals (
                    db_key TEXT NOT NULL,
                    total_key TEXT NOT NULL,
                    element TEXT NOT NULL,
                    accepted INTEGER NOT NULL,
                    PRIMARY KEY (db_key, total_key)
                );
                CREATE TABLE IF NOT EXISTS elements (
                    db_key TEXT NOT NULL,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    PRIMARY KEY (db_key, name, kind)
                );
                CREATE TABLE IF NOT EXISTS phases (
                    db_key TEXT NOT NULL,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    si_probe REAL,
                    formula TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (db_key, name)
                );
                CREATE TABLE IF NOT EXISTS species (
                    db_key TEXT NOT NULL,
                    element TEXT NOT NULL,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (db_key, element, name)
                );
                CREATE TABLE IF NOT EXISTS solid_aqueous_collisions (
                    db_key TEXT NOT NULL,
                    phase_name TEXT NOT NULL,
                    PRIMARY KEY (db_key, phase_name)
                );
                CREATE TABLE IF NOT EXISTS phase_elements (
                    db_key TEXT NOT NULL,
                    phase_name TEXT NOT NULL,
                    element TEXT NOT NULL,
                    PRIMARY KEY (db_key, phase_name, element)
                );
                """
            )
            # Migrate older catalogs that predate the formula column.
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(phases)").fetchall()
            }
            if "formula" not in cols:
                conn.execute(
                    "ALTER TABLE phases ADD COLUMN formula TEXT NOT NULL DEFAULT ''"
                )
            conn.commit()
        finally:
            conn.close()


def fingerprint_file(path: str | Path) -> tuple[int, int, str]:
    p = Path(path)
    stat = p.stat()
    digest = hashlib.sha256(p.read_bytes()).hexdigest()
    return stat.st_size, stat.st_mtime_ns, digest


def db_key_for(path: str | Path, sha256: str) -> str:
    return sha256


def get_status(db_key: str) -> CatalogStatus | None:
    with _LOCK:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT db_key, status, error, scanned_at, schema_version FROM databases WHERE db_key = ?",
                (db_key,),
            ).fetchone()
            if not row:
                return None
            return CatalogStatus(
                db_key=row["db_key"],
                status=row["status"],
                error=row["error"],
                scanned_at=row["scanned_at"],
                schema_version=int(row["schema_version"]),
            )
        finally:
            conn.close()


def is_fresh(rec: DatabaseRecord, db_key: str) -> bool:
    status = get_status(db_key)
    if not status or status.status != "ready":
        return False
    if status.schema_version != SCHEMA_VERSION:
        return False
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT size, mtime_ns, sha256 FROM databases WHERE db_key = ?",
            (db_key,),
        ).fetchone()
        if not row:
            return False
        size, mtime_ns, sha256 = fingerprint_file(rec.path)
        return (
            int(row["size"]) == size
            and int(row["mtime_ns"]) == mtime_ns
            and row["sha256"] == sha256
        )
    finally:
        conn.close()


def save_snapshot(
    rec: DatabaseRecord,
    snapshot: DatabaseCatalogSnapshot,
    *,
    db_key: str,
    size: int,
    mtime_ns: int,
    sha256: str,
) -> None:
    with _LOCK:
        conn = _connect()
        try:
            conn.execute("DELETE FROM databases WHERE db_key = ?", (db_key,))
            for table in (
                "totals",
                "elements",
                "phases",
                "species",
                "solid_aqueous_collisions",
                "phase_elements",
            ):
                conn.execute(f"DELETE FROM {table} WHERE db_key = ?", (db_key,))

            conn.execute(
                """
                INSERT INTO databases (
                    db_key, db_id, path, filename, source, size, mtime_ns, sha256,
                    status, error, scanned_at, schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ready', NULL, ?, ?)
                """,
                (
                    db_key,
                    rec.id,
                    rec.path,
                    rec.filename,
                    rec.source,
                    size,
                    mtime_ns,
                    sha256,
                    _utcnow(),
                    SCHEMA_VERSION,
                ),
            )

            for total in snapshot.accepted_totals:
                conn.execute(
                    "INSERT INTO totals (db_key, total_key, element, accepted) VALUES (?, ?, ?, 1)",
                    (db_key, total, element_from_total_key(total)),
                )

            for elem in snapshot.elements:
                conn.execute(
                    "INSERT INTO elements (db_key, name, kind) VALUES (?, ?, ?)",
                    (db_key, elem.name, elem.kind),
                )

            for phase in snapshot.solid_phases:
                formula = phase.formula or snapshot.phase_formulas.get(phase.name, "")
                conn.execute(
                    "INSERT INTO phases (db_key, name, kind, si_probe, formula) "
                    "VALUES (?, ?, 'solid', ?, ?)",
                    (db_key, phase.name, phase.si, formula),
                )
            for phase in snapshot.gas_phases:
                formula = phase.formula or snapshot.phase_formulas.get(phase.name, "")
                conn.execute(
                    "INSERT INTO phases (db_key, name, kind, si_probe, formula) "
                    "VALUES (?, ?, 'gas', ?, ?)",
                    (db_key, phase.name, phase.si, formula),
                )

            for element, names in snapshot.species_by_element.items():
                for name in names:
                    conn.execute(
                        "INSERT INTO species (db_key, element, name, kind) VALUES (?, ?, ?, 'aq')",
                        (db_key, element, name),
                    )

            for name in snapshot.solid_aqueous_collisions:
                conn.execute(
                    "INSERT INTO solid_aqueous_collisions (db_key, phase_name) VALUES (?, ?)",
                    (db_key, name),
                )

            for name, elems in snapshot.phase_elements.items():
                for element in elems:
                    conn.execute(
                        "INSERT INTO phase_elements (db_key, phase_name, element) VALUES (?, ?, ?)",
                        (db_key, name, element),
                    )

            conn.commit()
        finally:
            conn.close()


def save_failure(
    rec: DatabaseRecord,
    error: str,
    *,
    db_key: str,
    size: int,
    mtime_ns: int,
    sha256: str,
) -> None:
    with _LOCK:
        conn = _connect()
        try:
            conn.execute("DELETE FROM databases WHERE db_key = ?", (db_key,))
            conn.execute(
                """
                INSERT INTO databases (
                    db_key, db_id, path, filename, source, size, mtime_ns, sha256,
                    status, error, scanned_at, schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'failed', ?, ?, ?)
                """,
                (
                    db_key,
                    rec.id,
                    rec.path,
                    rec.filename,
                    rec.source,
                    size,
                    mtime_ns,
                    sha256,
                    error,
                    _utcnow(),
                    SCHEMA_VERSION,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def require_ready(rec: DatabaseRecord) -> str:
    size, mtime_ns, sha256 = fingerprint_file(rec.path)
    db_key = db_key_for(rec.path, sha256)
    status = get_status(db_key)
    if not status or status.status != "ready" or not is_fresh(rec, db_key):
        raise RuntimeError(
            f"Database catalog is not ready for {rec.name}. "
            f"Status={status.status if status else 'missing'}; "
            f"error={status.error if status else 'not scanned'}"
        )
    return db_key


def list_elements(db_key: str) -> list[str]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT DISTINCT element FROM totals WHERE db_key = ? AND accepted = 1 ORDER BY element",
            (db_key,),
        ).fetchall()
        return [r["element"] for r in rows]
    finally:
        conn.close()


def list_accepted_totals(db_key: str) -> list[str]:
    from ..phreeqc.catalog import normalize_total_keys

    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT total_key FROM totals WHERE db_key = ? AND accepted = 1 ORDER BY total_key",
            (db_key,),
        ).fetchall()
        # Normalize Fe(+3)→Fe(3) for older catalog rows written before spelling fix.
        return list(normalize_total_keys([r["total_key"] for r in rows]))
    finally:
        conn.close()


def _load_phase_elements(conn: sqlite3.Connection, db_key: str) -> dict[str, frozenset[str]]:
    """phase name -> element set for all phases with a known composition."""
    rows = conn.execute(
        "SELECT phase_name, element FROM phase_elements WHERE db_key = ?",
        (db_key,),
    ).fetchall()
    out: dict[str, set[str]] = {}
    for row in rows:
        out.setdefault(row["phase_name"], set()).add(row["element"])
    return {name: frozenset(elems) for name, elems in out.items()}


def phases_in_subset(
    phase_elements: dict[str, frozenset[str]],
    subset: set[str],
) -> list[str]:
    """Phase names whose constituent elements all fall within ``subset``.

    Phases with an empty element set are excluded (mirrors the prior parser
    behaviour: a phase must contribute at least one system element).
    """
    return sorted(
        name
        for name, elems in phase_elements.items()
        if elems and elems <= subset
    )


def list_phases(
    db_key: str,
    *,
    system_elements: set[str],
    selected: set[str] | None = None,
    exclude_gases: bool = False,
) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        phase_elements = _load_phase_elements(conn, db_key)
        eligible = set(phases_in_subset(phase_elements, set(system_elements)))

        kinds = dict(
            conn.execute(
                "SELECT name, kind FROM phases WHERE db_key = ?",
                (db_key,),
            ).fetchall()
        )
        si_probe = dict(
            conn.execute(
                "SELECT name, si_probe FROM phases WHERE db_key = ?",
                (db_key,),
            ).fetchall()
        )
        formulas = dict(
            conn.execute(
                "SELECT name, formula FROM phases WHERE db_key = ?",
                (db_key,),
            ).fetchall()
        )

        out: list[dict[str, Any]] = []
        for name in sorted(eligible):
            kind = kinds.get(name, "solid")
            if exclude_gases and (kind == "gas" or is_gas(name)):
                continue
            if selected is not None and name not in selected:
                continue
            out.append(
                {
                    "name": name,
                    "formula": formulas.get(name) or name,
                    "elements": sorted(phase_elements.get(name, frozenset())),
                    "kind": kind or "solid",
                    "si_probe": si_probe.get(name),
                }
            )
        if selected:
            missing = selected - {p["name"] for p in out}
            if missing:
                raise LookupError(
                    f"Selected phases not in catalog for this system: {sorted(missing)}"
                )
        return out
    finally:
        conn.close()


def list_collisions(db_key: str) -> frozenset[str]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT phase_name FROM solid_aqueous_collisions WHERE db_key = ?",
            (db_key,),
        ).fetchall()
        return frozenset(r["phase_name"] for r in rows)
    finally:
        conn.close()


def list_gas_phases(db_key: str) -> tuple[str, ...]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT name FROM phases WHERE db_key = ? AND kind = 'gas' ORDER BY name",
            (db_key,),
        ).fetchall()
        return tuple(r["name"] for r in rows)
    finally:
        conn.close()


def list_trace_gas_phases(
    db_key: str,
    system_elements: tuple[str, ...] | set[str],
) -> tuple[str, ...]:
    """Component gas phases (not O2/H2) whose elements overlap the system."""
    sys_set = set(system_elements)
    conn = _connect()
    try:
        phase_elements = _load_phase_elements(conn, db_key)
        out: list[str] = []
        for name in list_gas_phases(db_key):
            if name in ("O2(g)", "H2(g)"):
                continue
            if phase_elements.get(name, frozenset()) & sys_set:
                out.append(name)
        return tuple(out)
    finally:
        conn.close()


def phase_names_by_subset_map(
    db_key: str,
    system_elements: tuple[str, ...],
) -> dict[str, tuple[str, ...]]:
    """Eligible solid phases for every element subset of the system.

    Derived from stored phase element compositions via subset membership, so any
    subset (singles, pairs, triples, full system) resolves correctly without
    per-subset PHREEQC probing.
    """
    conn = _connect()
    try:
        phase_elements = _load_phase_elements(conn, db_key)
        out: dict[str, tuple[str, ...]] = {}
        for subset in subsets_for_scan(system_elements):
            sk = subset_key(subset)
            out[sk] = tuple(phases_in_subset(phase_elements, set(subset)))
        return out
    finally:
        conn.close()


def catalog_public_meta(rec: DatabaseRecord) -> dict[str, Any]:
    try:
        size, mtime_ns, sha256 = fingerprint_file(rec.path)
        db_key = db_key_for(rec.path, sha256)
    except OSError:
        return {"catalog_status": "missing", "catalog_error": "database file unreadable"}
    status = get_status(db_key)
    if not status:
        return {"catalog_status": "pending", "catalog_error": None}
    return {
        "catalog_status": status.status,
        "catalog_error": status.error,
        "catalog_scanned_at": status.scanned_at,
    }
