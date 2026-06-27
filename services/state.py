"""Persisted UI configuration on disk."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .. import config

_state_lock = threading.Lock()
STATE_FILE = config.PACKAGE_DIR / "saved_state.json"


def load_saved_state() -> dict[str, Any]:
    with _state_lock:
        if STATE_FILE.is_file():
            try:
                return {"found": True, "state": json.loads(STATE_FILE.read_text(encoding="utf-8"))}
            except (json.JSONDecodeError, OSError):
                return {"found": False, "state": None}
    return {"found": False, "state": None}


def save_saved_state(body: dict[str, Any]) -> dict[str, Any]:
    with _state_lock:
        STATE_FILE.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return {"saved": True, "path": str(STATE_FILE)}
