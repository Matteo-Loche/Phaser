"""Basic import/config smoke checks for local and container environments."""
from __future__ import annotations

import sys
from pathlib import Path


def _ensure_package_importable() -> None:
    package_dir = Path(__file__).resolve().parents[1]
    parent = package_dir.parent
    if str(parent) not in sys.path:
        sys.path.insert(0, str(parent))


def main() -> None:
    _ensure_package_importable()

    from PHASER.api.app import app
    from PHASER.chemistry.units import convert_concentration
    from PHASER.db.catalog_store import init_schema
    from PHASER.db.registry import get_default_database, list_databases

    init_schema()

    databases = list_databases()
    print(f"app: {app.title}")
    print(f"databases: {len(databases)}")
    if databases:
        default_db = get_default_database()
        print(f"default database: {default_db.id} ({default_db.name})")
    converted = convert_concentration(1.0, "umol/kgw", "mmol/kgw")
    assert converted == 0.001, converted
    converted2 = convert_concentration(1.0, "mmol/kgw", "mol/kgw")
    assert converted2 == 0.001, converted2
    print("unit conversion: ok")


if __name__ == "__main__":
    main()
