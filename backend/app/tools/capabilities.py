from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.state.schemas import Attachment
from app.tools.file_workspace import FileWorkspace, report_paths_for_run


SideEffect = Literal["none", "case_read", "case_write", "report_write", "file_extract"]
Idempotency = Literal["safe", "idempotent", "side_effectful"]
ContextExposure = Literal["summary_only", "artifact_ref", "full_result_allowed"]


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadCaseStateInput(ToolInput):
    pass


class ListCaseFilesInput(ToolInput):
    pass


class ReadAttachmentInput(ToolInput):
    name: str = ""
    path: str = ""
    attachment_id: str = ""
    original_ref: str = ""
    max_chars: int = 12000


class WriteCasePatchInput(ToolInput):
    patch: dict[str, Any] = Field(default_factory=dict)


class WriteCaseFileInput(ToolInput):
    relative_path: str = ""
    content: str = ""
    content_ref: str = ""


class RenderPdfInput(ToolInput):
    markdown_path: str = ""
    relative_path: str = ""
    pdf_path: str | None = None


@dataclass(frozen=True)
class ToolCallContext:
    workspace: FileWorkspace
    case_id: str
    attachments: list[Attachment]
    run_state: Any | None = None
    content_resolver: Any | None = None


ToolHandler = Callable[[ToolCallContext, ToolInput], dict[str, Any]]


@dataclass(frozen=True)
class ToolCapability:
    name: str
    input_model: type[ToolInput]
    handler: ToolHandler
    side_effect: SideEffect
    idempotency: Idempotency
    error_taxonomy: tuple[str, ...]
    context_exposure: ContextExposure
    precondition: str = ""
    postcondition: str = ""
    required_result_keys: tuple[str, ...] = ()

    def trace_metadata(self) -> dict[str, Any]:
        return {
            "side_effect": self.side_effect,
            "idempotency": self.idempotency,
            "context_exposure": self.context_exposure,
            "error_taxonomy": list(self.error_taxonomy),
            "precondition": self.precondition,
            "postcondition": self.postcondition,
        }


def _read_case_state(context: ToolCallContext, payload: ToolInput) -> dict[str, Any]:
    _ = payload
    return context.workspace.read_case_state(context.case_id)


def _read_attachment(context: ToolCallContext, payload: ToolInput) -> dict[str, Any]:
    data = _as(payload, ReadAttachmentInput)
    return context.workspace.read_attachment(
        context.case_id,
        context.attachments,
        name=data.name,
        path=data.path,
        attachment_id=data.attachment_id,
        original_ref=data.original_ref,
        max_chars=data.max_chars,
        session_id=str(getattr(context.run_state, "session_id", "") or ""),
        turn_id=str(getattr(context.run_state, "turn_id", "") or ""),
        run_id=str(getattr(context.run_state, "run_id", "") or ""),
    )


def _write_case_patch(context: ToolCallContext, payload: ToolInput) -> dict[str, Any]:
    data = _as(payload, WriteCasePatchInput)
    return context.workspace.write_case_patch(context.case_id, data.patch)


def _list_case_files(context: ToolCallContext, payload: ToolInput) -> dict[str, Any]:
    _ = payload
    return context.workspace.list_case_files(context.case_id)


def _write_case_file(context: ToolCallContext, payload: ToolInput) -> dict[str, Any]:
    data = _as(payload, WriteCaseFileInput)
    relative_path = data.relative_path
    content = data.content
    if data.content_ref.startswith("last_role:report_writer") or _looks_like_report_path(relative_path):
        relative_path, _ = report_paths_for_run(_started_at(context.run_state))
    if data.content_ref:
        if not context.content_resolver or context.run_state is None:
            raise ValueError("content_ref requires a content resolver and run_state")
        content = context.content_resolver(context.case_id, context.run_state, data.content_ref)
    return context.workspace.write_case_file(context.case_id, relative_path, content)


def _render_pdf(context: ToolCallContext, payload: ToolInput) -> dict[str, Any]:
    data = _as(payload, RenderPdfInput)
    markdown_path = data.markdown_path or data.relative_path
    pdf_path = data.pdf_path
    if _looks_like_report_path(markdown_path) or _looks_like_report_path(str(pdf_path or "")):
        markdown_path, pdf_path = report_paths_for_run(_started_at(context.run_state))
    return context.workspace.render_pdf(context.case_id, markdown_path, pdf_path)


def _as(payload: ToolInput, model: type[ToolInput]) -> Any:
    if not isinstance(payload, model):
        raise TypeError(f"Expected {model.__name__}, got {type(payload).__name__}")
    return payload


def _started_at(run_state: Any | None) -> str:
    return str(getattr(run_state, "started_at", "") or "")


def _looks_like_report_path(value: str) -> bool:
    text = str(value or "").replace("\\", "/").lower()
    if not text:
        return True
    name = text.rsplit("/", 1)[-1]
    if not text.startswith("reports/"):
        return False
    if name in {"final_report.md", "final_report.pdf", "report.md", "report.pdf", "manager_report.md", "manager_report.pdf"}:
        return True
    return name.startswith("final_report_") and (name.endswith(".md") or name.endswith(".pdf"))


TOOL_CAPABILITIES: dict[str, ToolCapability] = {
    "read_case_state": ToolCapability(
        name="read_case_state",
        input_model=ReadCaseStateInput,
        handler=_read_case_state,
        side_effect="case_read",
        idempotency="safe",
        error_taxonomy=("case_missing", "unsafe_workspace_path"),
        context_exposure="summary_only",
        precondition="case_id must resolve to the current workspace",
        postcondition="returns the current CaseState snapshot",
        required_result_keys=("case_id",),
    ),
    "read_attachment": ToolCapability(
        name="read_attachment",
        input_model=ReadAttachmentInput,
        handler=_read_attachment,
        side_effect="file_extract",
        idempotency="side_effectful",
        error_taxonomy=(
            "attachment_missing",
            "unsupported_attachment_type",
            "attachment_decode_error",
            "document_open_error",
            "ocr_unavailable",
            "unsafe_workspace_path",
        ),
        context_exposure="artifact_ref",
        precondition="attachment must be declared on this turn or resolvable from the case manifest",
        postcondition="stores original refs, extraction artifacts, previews, and manifest rows",
        required_result_keys=("case_id", "attachments", "attachment_manifest"),
    ),
    "write_case_patch": ToolCapability(
        name="write_case_patch",
        input_model=WriteCasePatchInput,
        handler=_write_case_patch,
        side_effect="case_write",
        idempotency="side_effectful",
        error_taxonomy=("patch_validation_error", "patch_input_too_large", "unsafe_workspace_path"),
        context_exposure="summary_only",
        precondition="must be invoked internally after a successful case_patch_writer observation",
        postcondition="persists a reduced CaseState update",
        required_result_keys=("case_id",),
    ),
    "list_case_files": ToolCapability(
        name="list_case_files",
        input_model=ListCaseFilesInput,
        handler=_list_case_files,
        side_effect="case_read",
        idempotency="safe",
        error_taxonomy=("case_missing", "unsafe_workspace_path"),
        context_exposure="summary_only",
        precondition="case_id must resolve to the current workspace",
        postcondition="returns relative case file paths",
        required_result_keys=("case_id", "files"),
    ),
    "write_case_file": ToolCapability(
        name="write_case_file",
        input_model=WriteCaseFileInput,
        handler=_write_case_file,
        side_effect="report_write",
        idempotency="side_effectful",
        error_taxonomy=("report_execution_wording", "unsafe_workspace_path", "content_ref_missing"),
        context_exposure="summary_only",
        precondition="relative_path must stay inside the case workspace; content_ref requires runtime resolver",
        postcondition="writes a case-local file and returns its relative path",
        required_result_keys=("case_id", "relative_path", "path"),
    ),
    "render_pdf": ToolCapability(
        name="render_pdf",
        input_model=RenderPdfInput,
        handler=_render_pdf,
        side_effect="report_write",
        idempotency="side_effectful",
        error_taxonomy=("render_source_missing", "pdf_render_error", "unsafe_workspace_path"),
        context_exposure="summary_only",
        precondition="markdown_path must point to a case-local Markdown report",
        postcondition="writes a case-local PDF and returns render metadata",
        required_result_keys=("case_id", "markdown_path", "pdf_path"),
    ),
}
