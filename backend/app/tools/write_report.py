from __future__ import annotations

from app.tools.file_workspace import FileWorkspace


def write_report(case_id: str, relative_path: str, content: str, workspace: FileWorkspace | None = None) -> dict[str, object]:
    return (workspace or FileWorkspace()).write_case_file(case_id, relative_path, content)
