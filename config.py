"""Default settings for the phase-diagram web service."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
from .__version__ import DOI_URL as _BUILTIN_DOI_URL
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

# User-generated databases (e.g. PyGCC output copied or registered here).
GENERATED_DB_DIR = Path(
    os.environ.get(
        "PHASER_GENERATED_DB_DIR",
        str(PACKAGE_DIR / "data" / "databases" / "generated"),
    )
)

# Optional explicit default database id from the registry.
DEFAULT_DB_ID = os.environ.get("PHASER_DEFAULT_DB_ID", "").strip() or None

# Builtin databases hidden from the UI/API (comma-separated stems or ids; empty = none).
# Values are slugified like registry ids (see db.registry._slugify). Source filenames
# in the IPhreeqc 3.8 bundle:
#   iso.dat, ColdChem.dat, frezchem.dat, Kinec.v2.dat, Kinec_v3.dat,
#   phreeqc_rates.dat, pitzer.dat, sit.dat
_DEFAULT_DISABLED_DB_STEMS = (
    "iso",           # iso.dat
    "coldchem",      # ColdChem.dat
    "frezchem",      # frezchem.dat
    "kinec-v2",      # Kinec.v2.dat
    "kinec-v3",      # Kinec_v3.dat
    "phreeqc-rates", # phreeqc_rates.dat
    "pitzer",        # pitzer.dat
    "sit",           # sit.dat
)


_DB_STEM_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _normalize_db_stem(value: str) -> str:
    return _DB_STEM_SLUG_RE.sub("-", value.lower()).strip("-")


def _load_disabled_db_stems() -> frozenset[str]:
    raw = os.environ.get("PHASER_DISABLED_DB_STEMS")
    if raw is None:
        return frozenset(_normalize_db_stem(s) for s in _DEFAULT_DISABLED_DB_STEMS)
    raw = raw.strip()
    if not raw:
        return frozenset()
    return frozenset(
        _normalize_db_stem(part)
        for part in raw.split(",")
        if part.strip()
    )


DISABLED_DB_STEMS: frozenset[str] = _load_disabled_db_stems()

# About / release metadata (shown in Statistics and /api/config).
BUILD_ID = os.environ.get("PHASER_BUILD_ID", "").strip() or None
_DEFAULT_REPO_URL = "https://github.com/matteo-loche/phaser"
REPOSITORY_URL = os.environ.get("PHASER_REPO_URL", _DEFAULT_REPO_URL).strip() or _DEFAULT_REPO_URL
_issues_override = os.environ.get("PHASER_ISSUES_URL", "").strip()
ISSUES_URL = _issues_override or f"{REPOSITORY_URL.rstrip('/')}/issues"


def _find_license_file() -> tuple[str, Path] | None:
    for filename in ("LICENSE", "LICENSE.txt", "LICENSE.md"):
        path = PACKAGE_DIR / filename
        if path.is_file():
            return filename, path
    return None


def _license_name_from_text(text: str) -> str:
    head = text[:2000].upper()
    if "AFFERO GENERAL PUBLIC LICENSE" in head and "VERSION 3" in head:
        return "AGPL-3.0"
    if "GNU GENERAL PUBLIC LICENSE" in head and "VERSION 3" in head:
        return "GPL-3.0"
    if "APACHE LICENSE" in head and "VERSION 2" in head:
        return "Apache-2.0"
    if "MIT LICENSE" in head:
        return "MIT"
    return "License"


_found_license = _find_license_file()
if _found_license:
    _license_filename, _license_path = _found_license
    _default_license_name = _license_name_from_text(
        _license_path.read_text(encoding="utf-8", errors="ignore")
    )
    _default_license_url = (
        f"{_DEFAULT_REPO_URL.rstrip('/')}/blob/main/{_license_filename}"
    )
else:
    _default_license_name = None
    _default_license_url = None

LICENSE_NAME = os.environ.get("PHASER_LICENSE_NAME", _default_license_name or "").strip() or None
_license_url_override = os.environ.get("PHASER_LICENSE_URL", "").strip()
LICENSE_URL = _license_url_override or _default_license_url

_doi_override = os.environ.get("PHASER_DOI_URL", "").strip()
_baked_doi = (_BUILTIN_DOI_URL or "").strip()
DOI_URL = _doi_override or _baked_doi or None

_WIN_IPHREEQC_CANDIDATES = (
    r"C:\Program Files\USGS\IPhreeqc\bin\IPhreeqcCOM.dll",
    r"C:\Program Files (x86)\USGS\IPhreeqc\bin\IPhreeqcCOM.dll",
)
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
    _first_existing(_WIN_IPHREEQC_CANDIDATES)
    if _IS_WINDOWS
    else _first_existing(_LINUX_IPHREEQC_CANDIDATES),
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

# Grid-point PHREEQC input modes (see phreeqc/input_titration.py, input_direct.py).
SOLUTION_MODE_DEFAULT = "titration"
SOLUTION_MODE_META: dict[str, dict[str, str]] = {
    "titration": {
        "label": "Charge-balanced titration",
        "description": (
            "Acidic seed solution charge-balanced with Cl⁻; pH fixed via Fix_H⁺/NaOH "
            "titration and redox via O₂(g) fugacity. Solution remains electroneutral."
        ),
    },
    "direct": {
        "label": "Direct (fixed pH–pe)",
        "description": (
            "Single SOLUTION at each grid point with the requested pH, pe, and totals. "
            "No charge balancing — electroneutrality may be violated."
        ),
    },
}
SOLUTION_MODES: tuple[str, ...] = tuple(SOLUTION_MODE_META.keys())

MAX_PHASES_PER_JOB = 200
MAX_GRID_POINTS = 40000  # 200 x 200
MAX_WORKERS = int(os.environ.get("PHASER_MAX_WORKERS", "8"))
MAX_CONCURRENT_JOBS = int(os.environ.get("PHASER_MAX_CONCURRENT_JOBS", "1"))

# When enabled, compute evaluates the full selected grid first, then traces
# phase boundaries on mixed cells via root-finding (see boundary_trace.py).
ADAPTIVE_BOUNDARIES_DEFAULT = True
# Subdivision factor for the local fallback sub-grid inside unresolved cells and
# for the fine display raster in diagram/vectors.py (exact line/region SDF fills).
ADAPTIVE_REFINE_FACTOR = int(os.environ.get("PHASER_ADAPTIVE_REFINE_FACTOR", "5"))
# Soft cap on total PHREEQC evaluations in adaptive mode (base grid + trace work).
MAX_ADAPTIVE_POINTS = int(os.environ.get("PHASER_MAX_ADAPTIVE_POINTS", "120000"))
# Aqueous species molalities punched per element (USER_PUNCH via SYS) for
# predominance and tracing. High count so minor species are not missed.
TOP_AQ_SPECIES_PER_ELEMENT = int(os.environ.get("PHASER_TOP_AQ_SPECIES", "64"))
# Top species kept PER ELEMENT at each grid point for context-filtered hover.
# Bounded per element so subset filtering stays exact without bloating JSON.
HOVER_SPECIES_PER_ELEMENT = int(os.environ.get("PHASER_HOVER_SPECIES_PER_ELEMENT", "4"))
# Trace mode uses fewer USER_PUNCH slots; corner/boundary species stay on explicit -mol.
BOUNDARY_TRACE_TOP_AQ_SPECIES = int(
    os.environ.get("PHASER_TRACE_TOP_AQ_SPECIES", "4")
)
# Relative tolerance for 1D root finding along cell edges (trace mode).
BOUNDARY_TRACE_TOLERANCE = float(os.environ.get("PHASER_BOUNDARY_TRACE_TOLERANCE", "1e-4"))
# Trace multiprocessing: submit workers×multiplier small jobs for pool load-balancing.
TRACE_CHUNK_MULTIPLIER = int(os.environ.get("PHASER_TRACE_CHUNK_MULTIPLIER", "8"))
TRACE_MIN_CELLS_PER_CHUNK = int(os.environ.get("PHASER_TRACE_MIN_CELLS_PER_CHUNK", "8"))
# Base grid sweep: ProcessPoolExecutor.map chunksize (points per IPC message).
SWEEP_MAP_CHUNKSIZE = int(os.environ.get("PHASER_SWEEP_MAP_CHUNKSIZE", "200"))

# Completed job results are dropped from server memory after this TTL if the
# browser never fetched them (or after fetch + DELETE). Also used by the reaper.
JOB_RESULT_TTL_SEC = int(os.environ.get("PHASER_JOB_RESULT_TTL_SEC", "3600"))
# Queued jobs that were never polled are removed (abandoned tab / never returned).
JOB_QUEUE_TTL_SEC = int(os.environ.get("PHASER_JOB_QUEUE_TTL_SEC", "7200"))
JOB_REAPER_INTERVAL_SEC = int(os.environ.get("PHASER_JOB_REAPER_INTERVAL_SEC", "60"))

# Water-stability gas limits (atm) for O₂/H₂ diagram boundaries and labels.
# Override with PHASER_O2_LIMIT_ATM / PHASER_H2_LIMIT_ATM; UI can set per job.
O2_FUGACITY_LIMIT_ATM = float(os.environ.get("PHASER_O2_LIMIT_ATM", "0.21"))
H2_FUGACITY_LIMIT_ATM = float(os.environ.get("PHASER_H2_LIMIT_ATM", "1.0"))
COMPONENT_GAS_FUGACITY_LIMIT_ATM = float(os.environ.get("PHASER_COMPONENT_GAS_LIMIT_ATM", "1.0"))

# UI/API concentration units (mol/kgw basis). PHREEQC SOLUTION blocks always
# receive mmol/kgw (DEFAULT_UNITS); values are converted before engine input.
UNIT_OPTIONS = ("mol/kgw", "mmol/kgw", "umol/kgw")

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

STATS_DB = Path(
    os.environ.get("PHASER_STATS_DB", str(PACKAGE_DIR / "data" / "stats.sqlite"))
)

# Per-client API rate limits (sliding window). 0 on a bucket disables that cap.
# All /api/* routes share the general "api" bucket; expensive POSTs also hit
# tighter route-specific buckets. /api/health is exempt (Docker probes).
def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


RATE_LIMIT_ENABLED = _env_bool("PHASER_RATE_LIMIT", True)
RATE_LIMIT_WINDOW_SEC = int(os.environ.get("PHASER_RATE_LIMIT_WINDOW_SEC", "60"))
RATE_LIMIT_API_PER_MIN = int(os.environ.get("PHASER_RATE_LIMIT_API_PER_MIN", "600"))
RATE_LIMIT_COMPUTE_PER_MIN = int(os.environ.get("PHASER_RATE_LIMIT_COMPUTE_PER_MIN", "12"))
RATE_LIMIT_DB_REGISTER_PER_MIN = int(
    os.environ.get("PHASER_RATE_LIMIT_DB_REGISTER_PER_MIN", "6")
)
RATE_LIMIT_PHASES_PER_MIN = int(os.environ.get("PHASER_RATE_LIMIT_PHASES_PER_MIN", "60"))
# After tripping a route burst cap, block that client on the route for this duration.
RATE_LIMIT_COMPUTE_COOLDOWN_SEC = int(
    os.environ.get("PHASER_RATE_LIMIT_COMPUTE_COOLDOWN_SEC", "600")
)
RATE_LIMIT_DB_REGISTER_COOLDOWN_SEC = int(
    os.environ.get("PHASER_RATE_LIMIT_DB_REGISTER_COOLDOWN_SEC", "300")
)
RATE_LIMIT_COOLDOWN_ESCALATE = _env_bool("PHASER_RATE_LIMIT_COOLDOWN_ESCALATE", True)
RATE_LIMIT_COOLDOWN_MAX_SEC = int(os.environ.get("PHASER_RATE_LIMIT_COOLDOWN_MAX_SEC", "3600"))
RATE_LIMIT_VIOLATION_RESET_SEC = int(
    os.environ.get("PHASER_RATE_LIMIT_VIOLATION_RESET_SEC", "86400")
)

# Default total concentration for catalog SYS probes (units = DEFAULT_UNITS).
# With mmol/kgw, 1.0 means 1 mmol/kgw per accepted total key.
CATALOG_PROBE_AMOUNT = float(os.environ.get("PHASER_CATALOG_PROBE_AMOUNT", "1.0"))
