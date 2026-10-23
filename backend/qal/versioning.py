from __future__ import annotations

import hashlib
import json
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from qal import __version__


@lru_cache
def code_version() -> str:
    """Git commit of the running code (with -dirty marker) or the package version."""
    root = Path(__file__).resolve().parents[2]
    try:
        sha = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=root, capture_output=True,
                             text=True, timeout=5, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "backend/qal"], cwd=root,
                               capture_output=True, text=True, timeout=5).stdout.strip()
        return f"git:{sha}{'-dirty' if dirty else ''}"
    except Exception:
        return f"pkg:{__version__}"


def stable_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()
