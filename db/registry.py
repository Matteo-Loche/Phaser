"""Server-side registry of trusted PHREEQC database files."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from .. import config

DatabaseSource = Literal["builtin", "generated", "registered"]

_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class DatabaseRecord:
    id: str
    name: str
    path: str
    source: DatabaseSource
    exists: bool
    filename: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        """Client-safe payload (no filesystem path)."""
        out: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "source": self.source,
            "exists": self.exists,
            "filename": self.filename,
        }
        if self.metadata:
            out["metadata"] = {
                k: v
                for k, v in self.metadata.items()
                if k not in {"path", "absolute_path"}
            }
        return out


def _slugify(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-")


def _make_id(source: DatabaseSource, filename: str, used: set[str]) -> str:
    base = f"{source}-{_slugify(Path(filename).stem)}"
    candidate = base
    n = 2
    while candidate in used:
        candidate = f"{base}-{n}"
        n += 1
    used.add(candidate)
    return candidate


def _load_sidecar_metadata(dat_path: Path) -> dict[str, Any]:
    sidecar = dat_path.with_suffix(".meta.json")
    if not sidecar.is_file():
        return {}
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _display_name(dat_path: Path, metadata: dict[str, Any]) -> str:
    if metadata.get("name"):
        return str(metadata["name"])
    return dat_path.stem


def _scan_directory(
    directory: Path,
    *,
    source: DatabaseSource,
    used_ids: set[str],
) -> list[DatabaseRecord]:
    if not directory.is_dir():
        return []

    records: list[DatabaseRecord] = []
    for dat_path in sorted(directory.glob("*.dat")):
        metadata = _load_sidecar_metadata(dat_path)
        db_id = metadata.get("id") or _make_id(source, dat_path.name, used_ids)
        resolved = dat_path.resolve()
        records.append(
            DatabaseRecord(
                id=str(db_id),
                name=_display_name(dat_path, metadata),
                path=str(resolved),
                source=source,
                exists=resolved.is_file(),
                filename=dat_path.name,
                metadata=metadata,
            )
        )
    return records


def _ensure_default_registered(
    records: list[DatabaseRecord],
    used_ids: set[str],
) -> list[DatabaseRecord]:
    """Ensure the configured default Thermoddem database appears in the registry."""
    default_path = Path(config.THERMODDEM_DB)
    if not default_path.is_file():
        return records

    resolved = str(default_path.resolve())
    for rec in records:
        if Path(rec.path).resolve() == Path(resolved):
            return records

    metadata = _load_sidecar_metadata(default_path)
    db_id = metadata.get("id") or _make_id("builtin", default_path.name, used_ids)
    records.append(
        DatabaseRecord(
            id=str(db_id),
            name=_display_name(default_path, metadata) or "Thermoddem (default)",
            path=resolved,
            source="builtin",
            exists=True,
            filename=default_path.name,
            metadata=metadata,
        )
    )
    return records


def _database_match_keys(rec: DatabaseRecord) -> set[str]:
    keys: set[str] = {_slugify(Path(rec.filename).stem), rec.id.lower()}
    rid = rec.id.lower()
    for prefix in ("builtin-", "generated-", "registered-"):
        if rid.startswith(prefix):
            keys.add(rid[len(prefix):])
    return keys


def is_database_disabled(rec: DatabaseRecord) -> bool:
    disabled = config.DISABLED_DB_STEMS
    if not disabled:
        return False
    return bool(_database_match_keys(rec) & disabled)


@lru_cache(maxsize=1)
def list_databases(*, refresh_token: int = 0) -> tuple[DatabaseRecord, ...]:
    del refresh_token  # cache-bust via invalidate_registry()
    used_ids: set[str] = set()
    records: list[DatabaseRecord] = []

    for directory in config.BUILTIN_DB_DIRS:
        records.extend(_scan_directory(Path(directory), source="builtin", used_ids=used_ids))

    records.extend(
        _scan_directory(config.GENERATED_DB_DIR, source="generated", used_ids=used_ids)
    )

    records = _ensure_default_registered(records, used_ids)
    records.sort(key=lambda r: (r.source != "builtin", r.source, r.name.lower()))
    return tuple(records)


def list_enabled_databases(*, refresh_token: int = 0) -> tuple[DatabaseRecord, ...]:
    return tuple(r for r in list_databases(refresh_token=refresh_token) if not is_database_disabled(r))


def invalidate_registry() -> None:
    list_databases.cache_clear()


def get_database(db_id: str) -> DatabaseRecord | None:
    for rec in list_databases():
        if rec.id == db_id:
            return rec
    return None


def get_default_database() -> DatabaseRecord:
    records = [r for r in list_enabled_databases() if r.exists]
    if not records:
        raise RuntimeError(
            "No PHREEQC databases are available on the server. "
            "Install PHREEQC databases or add files to the generated database directory."
        )

    preferred = config.DEFAULT_DB_ID
    if preferred:
        match = get_database(preferred)
        if match and match.exists and not is_database_disabled(match):
            return match

    default_path = Path(config.THERMODDEM_DB)
    if default_path.is_file():
        resolved = str(default_path.resolve())
        for rec in records:
            if Path(rec.path).resolve() == Path(resolved):
                return rec

    return records[0]


def find_database_by_path(path: str | Path) -> DatabaseRecord | None:
    target = Path(path).resolve()
    for rec in list_databases():
        if Path(rec.path).resolve() == target:
            return rec
    return None


def resolve_database(
    db_id: str | None = None,
    db_path: str | None = None,
) -> DatabaseRecord:
    """Resolve a trusted database record from id (preferred) or legacy path."""
    if db_id:
        rec = get_database(db_id)
        if not rec:
            raise LookupError(f"Database id not found: {db_id}")
        if is_database_disabled(rec):
            raise LookupError(f"Database is disabled: {rec.name}")
        if not rec.exists:
            raise LookupError(f"Database unavailable: {rec.name}")
        return rec

    if db_path:
        rec = find_database_by_path(db_path)
        if rec and rec.exists:
            if is_database_disabled(rec):
                raise LookupError(f"Database is disabled: {rec.name}")
            return rec
        raise LookupError(
            "Unknown database path. Select a database from the server list."
        )

    return get_default_database()


def register_generated_database(
    dat_path: Path | str,
    *,
    metadata: dict[str, Any] | None = None,
    write_sidecar: bool = True,
) -> DatabaseRecord:
    """Register a user-generated database for future PyGCC / external tooling integration."""
    path = Path(dat_path)
    if not path.is_file():
        raise FileNotFoundError(f"Database file not found: {path}")

    config.GENERATED_DB_DIR.mkdir(parents=True, exist_ok=True)
    target = config.GENERATED_DB_DIR / path.name
    if path.resolve() != target.resolve():
        target.write_bytes(path.read_bytes())

    meta = dict(metadata or {})
    meta.setdefault("name", path.stem)
    meta.setdefault("origin_service", meta.get("origin_service", "external"))
    meta.setdefault("created_at", datetime.now(timezone.utc).isoformat())

    if write_sidecar:
        sidecar = target.with_suffix(".meta.json")
        sidecar.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    invalidate_registry()
    rec = find_database_by_path(target)
    if not rec:
        raise RuntimeError(f"Failed to register database: {target}")
    return rec
