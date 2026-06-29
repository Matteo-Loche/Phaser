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
GRID_LEVELS = 100  # single resolution for both pH and pe/Eh axes

MAX_PHASES_PER_JOB = 200
MAX_GRID_POINTS = 40000  # 200 x 200
MAX_WORKERS = 8
MAX_CONCURRENT_JOBS = int(os.environ.get("PHASER_MAX_CONCURRENT_JOBS", "1"))

# When enabled, compute evaluates the full selected grid first, then traces
# phase boundaries on mixed cells via root-finding (see boundary_trace.py).
ADAPTIVE_BOUNDARIES_DEFAULT = True
# Subdivision factor for the local fallback sub-grid inside unresolved cells and
# for the fine display raster in diagram/vectors.py (exact line/region SDF fills).
ADAPTIVE_REFINE_FACTOR = int(os.environ.get("PHASER_ADAPTIVE_REFINE_FACTOR", "5"))
# Soft cap on total PHREEQC evaluations in adaptive mode (base grid + trace work).
MAX_ADAPTIVE_POINTS = int(os.environ.get("PHASER_MAX_ADAPTIVE_POINTS", "120000"))
# Aqueous species molalities punched per element (USER_PUNCH via SYS) for tracing.
TOP_AQ_SPECIES_PER_ELEMENT = int(os.environ.get("PHASER_TOP_AQ_SPECIES", "8"))
# Trace mode uses fewer USER_PUNCH slots; corner/boundary species stay on explicit -mol.
BOUNDARY_TRACE_TOP_AQ_SPECIES = int(
    os.environ.get("PHASER_TRACE_TOP_AQ_SPECIES", "4")
)
# Relative tolerance for 1D root finding along cell edges (trace mode).
BOUNDARY_TRACE_TOLERANCE = float(os.environ.get("PHASER_BOUNDARY_TRACE_TOLERANCE", "1e-4"))
# Trace multiprocessing: submit workers×multiplier small jobs for pool load-balancing.
TRACE_CHUNK_MULTIPLIER = int(os.environ.get("PHASER_TRACE_CHUNK_MULTIPLIER", "8"))
TRACE_MIN_CELLS_PER_CHUNK = int(os.environ.get("PHASER_TRACE_MIN_CELLS_PER_CHUNK", "8"))

# Completed job results are dropped from server memory after this TTL if the
# browser never fetched them (or after fetch + DELETE). Also used by the reaper.
JOB_RESULT_TTL_SEC = int(os.environ.get("PHASER_JOB_RESULT_TTL_SEC", "3600"))
# Queued jobs that were never polled are removed (abandoned tab / never returned).
JOB_QUEUE_TTL_SEC = int(os.environ.get("PHASER_JOB_QUEUE_TTL_SEC", "7200"))
JOB_REAPER_INTERVAL_SEC = int(os.environ.get("PHASER_JOB_REAPER_INTERVAL_SEC", "60"))

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

# Broader parser-free candidate totals for full database catalog scans.
_CATALOG_ELEMENTS = (
    "H", "He", "Li", "Be", "B", "C", "N", "O", "F", "Ne", "Na", "Mg", "Al", "Si", "P",
    "S", "Cl", "Ar", "K", "Ca", "Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu",
    "Zn", "Ga", "Ge", "As", "Se", "Br", "Kr", "Rb", "Sr", "Y", "Zr", "Nb", "Mo", "Tc",
    "Ru", "Rh", "Pd", "Ag", "Cd", "In", "Sn", "Sb", "Te", "I", "Xe", "Cs", "Ba", "La",
    "Ce", "Pr", "Nd", "Pm", "Sm", "Eu", "Gd", "Tb", "Dy", "Ho", "Er", "Tm", "Yb", "Lu",
    "Hf", "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg", "Tl", "Pb", "Bi", "Po", "At",
    "Rn", "Fr", "Ra", "Ac", "Th", "Pa", "U", "Np", "Pu", "Am", "Cm", "Bk", "Cf", "Es",
    "Fm", "Md", "No", "Lr",
)
_CATALOG_REDOX_ALIASES = (
    "C(4)", "C(-4)", "N(5)", "N(3)", "N(0)", "N(-3)",
    "S(6)", "S(4)", "S(0)", "S(-2)",
    "P(5)", "P(3)", "P(-3)",
    "Fe(2)", "Fe(3)", "Cu(1)", "Cu(2)", "Mn(2)", "Mn(3)", "Mn(4)", "Mn(6)", "Mn(7)",
    "Cr(3)", "Cr(6)", "Cl(-1)", "As(3)", "As(5)",
)
CATALOG_TOTAL_CANDIDATES = tuple(dict.fromkeys((*_CATALOG_ELEMENTS, *_CATALOG_REDOX_ALIASES)))

CATALOG_DB = Path(
    os.environ.get("PHASER_CATALOG_DB", str(PACKAGE_DIR / "data" / "catalog.sqlite"))
)

# Default total concentration for catalog SYS probes (units = DEFAULT_UNITS).
# With mmol/kgw, 1.0 means 1 mmol/kgw per accepted total key.
CATALOG_PROBE_AMOUNT = float(os.environ.get("PHASER_CATALOG_PROBE_AMOUNT", "1.0"))
