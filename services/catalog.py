"""Catalog scan orchestration for startup and registration."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Literal

from .. import config
from ..db.catalog_store import (
    db_key_for,
    fingerprint_file,
    init_schema,
    is_fresh,
    save_failure,
    save_snapshot,
)
from ..db.registry import DatabaseRecord, get_default_database, list_enabled_databases
from ..phreeqc.catalog import open_phreeqc, scan_database_catalog

ScanStatus = Literal["pass", "cached", "fail"]

_bg_started = False
_bg_lock = threading.Lock()


def _catalog_log(message: str) -> None:
    print(f"[phaser catalog] {message}", flush=True)


def scan_database_record(
    rec: DatabaseRecord,
    *,
    dll_path: str | None = None,
    report: bool = False,
) -> ScanStatus:
    if not rec.exists:
        raise FileNotFoundError(f"Database unavailable: {rec.path}")
    dll = dll_path or config.IPHREEQC_DLL
    if not Path(dll).is_file():
        raise RuntimeError(f"IPhreeqc library not found: {dll}")

    label = rec.filename or rec.name
    if report:
        _catalog_log(f"scanning {label} ({rec.id})...")

    size, mtime_ns, sha256 = fingerprint_file(rec.path)
    db_key = db_key_for(rec.path, sha256)
    if is_fresh(rec, db_key):
        if report:
            _catalog_log(f"  cached — {label} (fingerprint fresh)")
        return "cached"

    try:
        pq = open_phreeqc(rec.path, dll)
        snapshot = scan_database_catalog(pq, rec.path)
        save_snapshot(
            rec,
            snapshot,
            db_key=db_key,
            size=size,
            mtime_ns=mtime_ns,
            sha256=sha256,
        )
        if report:
            _catalog_log(
                f"  pass — {label}: "
                f"{len(snapshot.solid_phases)} solids, "
                f"{len(snapshot.gas_phases)} gases, "
                f"{snapshot.species_count} species, "
                f"{len(snapshot.solid_aqueous_collisions)} collisions"
            )
        return "pass"
    except Exception as exc:
        save_failure(
            rec,
            str(exc),
            db_key=db_key,
            size=size,
            mtime_ns=mtime_ns,
            sha256=sha256,
        )
        if report:
            err = str(exc).strip().splitlines()[0]
            _catalog_log(f"  fail — {label}: {err}")
        raise


def _scan_background(records: list[DatabaseRecord], dll_path: str) -> None:
    counts = {"pass": 0, "cached": 0, "fail": 0}
    for rec in records:
        if not rec.exists:
            continue
        try:
            status = scan_database_record(rec, dll_path=dll_path, report=True)
            counts[status] += 1
        except Exception:
            counts["fail"] += 1
    _catalog_log(
        "background scan complete for non-default databases: "
        f"{counts['pass']} passed, "
        f"{counts['cached']} cached, "
        f"{counts['fail']} failed "
        f"({sum(counts.values())} total)"
    )


def initialize_catalogs(*, dll_path: str | None = None) -> None:
    """Initialize SQLite schema and scan databases."""
    global _bg_started
    _catalog_log(f"SQLite catalog: {config.CATALOG_DB}")
    init_schema()

    dll = dll_path or config.IPHREEQC_DLL
    if not Path(dll).is_file():
        _catalog_log(f"skip — IPhreeqc library not found: {dll}")
        return

    existing = [r for r in list_enabled_databases() if r.exists]
    _catalog_log(f"found {len(existing)} database(s) in registry")

    default = get_default_database()
    _catalog_log(f"default database: {default.filename} ({default.id})")
    default_status = scan_database_record(default, dll_path=dll, report=True)

    others = [r for r in existing if r.path != default.path]
    if others:
        _catalog_log(
            f"starting background scan of {len(others)} non-default database(s) "
            f"(default {default_status})..."
        )
        with _bg_lock:
            if not _bg_started:
                _bg_started = True
                thread = threading.Thread(
                    target=_scan_background,
                    args=(others, dll),
                    name="phaser-catalog-scan",
                    daemon=True,
                )
                thread.start()
    else:
        word = "passed" if default_status == "pass" else "cached"
        _catalog_log(f"startup scan complete: 1 {word} (1 total)")
