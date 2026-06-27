"""Default settings for the phase-diagram web service."""
from __future__ import annotations

import os
import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
_IS_WINDOWS = sys.platform == "win32"

# Override with PHASER_DB and PHASER_IPHREEQC_LIB if needed.
_WIN_THERMODDEM_DB = (
    r"C:\Program Files (x86)\USGS\Phreeqc Interactive 3.8.6-17100\database"
    r"\PHREEQC_ThermoddemV1.10_15Dec2020.dat"
)
_LINUX_THERMODDEM_DB = (
    "/mnt/c/Program Files (x86)/USGS/Phreeqc Interactive 3.8.6-17100/database"
    "/PHREEQC_ThermoddemV1.10_15Dec2020.dat"
)

THERMODDEM_DB = os.environ.get(
    "PHASER_DB",
    _WIN_THERMODDEM_DB if _IS_WINDOWS else _LINUX_THERMODDEM_DB,
)

_WIN_PHREEQC_DB_DIR = (
    r"C:\Program Files (x86)\USGS\Phreeqc Interactive 3.8.6-17100\database"
)
_LINUX_PHREEQC_DB_DIR = (
    "/mnt/c/Program Files (x86)/USGS/Phreeqc Interactive 3.8.6-17100/database"
)

# Directories scanned for built-in PHREEQC databases (comma-separated override).
_BUILTIN_DB_DIR_DEFAULT = (
    _WIN_PHREEQC_DB_DIR if _IS_WINDOWS else _LINUX_PHREEQC_DB_DIR
)
_extra_builtin_dirs = [
    p.strip()
    for p in os.environ.get("PHASER_BUILTIN_DB_DIRS", "").split(os.pathsep)
    if p.strip()
]
BUILTIN_DB_DIRS: tuple[Path, ...] = tuple(
    Path(p)
    for p in ([_BUILTIN_DB_DIR_DEFAULT, *_extra_builtin_dirs])
)

# User-generated databases (e.g. future PyGCC output copied or registered here).
GENERATED_DB_DIR = Path(
    os.environ.get(
        "PHASER_GENERATED_DB_DIR",
        str(PACKAGE_DIR / "data" / "databases" / "generated"),
    )
)

# Optional explicit default database id from the registry.
DEFAULT_DB_ID = os.environ.get("PHASER_DEFAULT_DB_ID", "").strip() or None

_WIN_IPHREEQC = r"C:\Users\Matteo\Documents\PhreeqPy\IPhreeqcCOM.dll"
_LINUX_IPHREEQC_CANDIDATES = (
    "/usr/local/lib/libiphreeqc.so",
    "/usr/lib/x86_64-linux-gnu/libiphreeqc.so",
    "/usr/lib/libiphreeqc.so",
)


def _first_existing(paths: tuple[str, ...]) -> str:
    for path in paths:
        if Path(path).is_file():
            return path
    return paths[0]


IPHREEQC_DLL = os.environ.get(
    "PHASER_IPHREEQC_LIB",
    _WIN_IPHREEQC if _IS_WINDOWS else _first_existing(_LINUX_IPHREEQC_CANDIDATES),
)

HOST = os.environ.get("PHASER_HOST", "0.0.0.0")
PORT = int(os.environ.get("PHASER_PORT", "8765"))

TEMP_C = 25.0
WATER_MASS_KGW = 1.0

# Default concentration entry in the UI.
DEFAULT_UNITS = "mmol/kgw"
DEFAULT_SPECIES_CONC = 1.0

# Default grid if client omits values.
PH_MIN = 2.0
PH_MAX = 12.0
PE_MIN = -10.0
PE_MAX = 14.0
GRID_LEVELS = 60  # single resolution for both pH and pe/Eh axes

MAX_PHASES_PER_JOB = 200
MAX_GRID_POINTS = 40000  # 200 x 200
MAX_WORKERS = 8
MAX_CONCURRENT_JOBS = int(os.environ.get("PHASER_MAX_CONCURRENT_JOBS", "1"))

# Concentration unit options passed straight to PHREEQC SOLUTION blocks.
UNIT_OPTIONS = (
    "mol/kgw", "mmol/kgw", "umol/kgw",
    "g/kgw", "mg/kgw", "ug/kgw",
    "mol/l", "mmol/l", "umol/l",
    "g/l", "mg/l", "ug/l", "ppm",
)

# Master species labels accepted in the UI (PHREEQC input names).
KNOWN_TOTALS = (
    "Fe", "Ca", "Mg", "Na", "K", "Al", "Si", "C(4)", "S(6)", "S(-2)",
    "Cl", "N(5)", "P", "Mn", "Zn", "Cu", "Pb", "Ba", "Sr", "Ni", "Cr",
)
