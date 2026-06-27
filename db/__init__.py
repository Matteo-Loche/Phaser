"""PHREEQC database discovery, registry, and parsing."""
from .parser import (
    COMMON_GASES,
    PhaseRecord,
    filter_phases,
    is_gas,
    list_common_gases,
    list_elements,
    load_phase_catalog,
    parse_phases,
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
    "COMMON_GASES",
    "DatabaseRecord",
    "DatabaseSource",
    "PhaseRecord",
    "filter_phases",
    "find_database_by_path",
    "get_database",
    "get_default_database",
    "invalidate_registry",
    "is_gas",
    "list_common_gases",
    "list_databases",
    "list_elements",
    "load_phase_catalog",
    "parse_phases",
    "register_generated_database",
    "resolve_database",
]
