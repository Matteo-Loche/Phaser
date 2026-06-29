"""PHREEQC database discovery, registry, and SQLite catalog cache."""
from .catalog_store import (
    catalog_public_meta,
    fingerprint_file,
    get_status,
    init_schema,
    is_fresh,
    list_collisions,
    list_elements,
    list_phases,
    require_ready,
)
from .registry import (
    DatabaseRecord,
    DatabaseSource,
    find_database_by_path,
    get_database,
    get_default_database,
    invalidate_registry,
    list_databases,
    register_generated_database,
    resolve_database,
)

__all__ = [
    "DatabaseRecord",
    "DatabaseSource",
    "catalog_public_meta",
    "find_database_by_path",
    "fingerprint_file",
    "get_database",
    "get_default_database",
    "get_status",
    "init_schema",
    "invalidate_registry",
    "is_fresh",
    "list_collisions",
    "list_databases",
    "list_elements",
    "list_phases",
    "register_generated_database",
    "require_ready",
    "resolve_database",
]
