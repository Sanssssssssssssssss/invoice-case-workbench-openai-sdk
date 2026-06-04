from __future__ import annotations

from typing import Any

from app.state.schemas import Attachment
from app.tools.file_workspace import FileWorkspace


def read_attachment(
    case_id: str,
    attachments: list[Attachment],
    payload: dict[str, Any] | None = None,
    workspace: FileWorkspace | None = None,
) -> dict[str, Any]:
    payload = payload or {}
    return (workspace or FileWorkspace()).read_attachment(
        case_id,
        attachments,
        name=str(payload.get("name") or ""),
        path=str(payload.get("path") or ""),
        attachment_id=str(payload.get("attachment_id") or ""),
        original_ref=str(payload.get("original_ref") or ""),
        max_chars=int(payload.get("max_chars") or 12000),
    )
