from __future__ import annotations

from typing import Any


RESOLVED_CONFLICT_STATUSES = {"resolved", "superseded", "closed"}


def resolved_conflict_note(value: Any) -> bool:
    if hasattr(value, "model_dump"):
        value = value.model_dump(exclude_none=True)
    if not isinstance(value, dict):
        return False
    return str(value.get("resolution_status") or "").strip().lower() in RESOLVED_CONFLICT_STATUSES
