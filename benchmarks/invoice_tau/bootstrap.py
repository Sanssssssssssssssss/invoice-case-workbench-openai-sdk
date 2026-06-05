from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"


def ensure_paths() -> None:
    for path in (REPO_ROOT, BACKEND_DIR):
        text = str(path)
        if text not in sys.path:
            sys.path.insert(0, text)
