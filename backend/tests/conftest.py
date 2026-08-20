from __future__ import annotations

import os
import sys
from pathlib import Path


# The default test suite is offline. Cached Hugging Face assets may be used,
# but tests must not turn a missing metadata request into a network timeout.
os.environ.setdefault("HF_HUB_OFFLINE", "1")

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
