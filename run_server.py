"""Start the phase-diagram web service."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running directly (``python run_server.py``) from inside the package
# folder by ensuring the parent directory is importable as a package root.
_PACKAGE_DIR = Path(__file__).resolve().parent
_PARENT = _PACKAGE_DIR.parent
_PACKAGE_NAME = _PACKAGE_DIR.name
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

import uvicorn

from importlib import import_module

config = import_module(f"{_PACKAGE_NAME}.config")


def main():
    parser = argparse.ArgumentParser(description="Phase diagram web service")
    parser.add_argument("--host", default=config.HOST)
    parser.add_argument("--port", type=int, default=config.PORT)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    uvicorn.run(
        f"{_PACKAGE_NAME}.api.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
