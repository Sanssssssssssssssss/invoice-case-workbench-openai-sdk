from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.state.schemas import ReviewMode


class _SupervisorToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MaterialsAdvisorInput(_SupervisorToolInput):
    question_focus: str = Field(default="", description="Optional short focus for the material/rule/template question.")


class EvidenceReviewerInput(_SupervisorToolInput):
    mode: ReviewMode = Field(default="review", description="extract, review, or repair")
    target_evidence_id: str = Field(default="", description="Evidence id for repair/recheck, if known.")
    target_attachment_id: str = Field(default="", description="Attachment id for repair/recheck, if known.")
    user_correction: str = Field(default="", description="User correction text for repair mode.")


class CasePatchWriterInput(_SupervisorToolInput):
    note: str = Field(default="", description="Optional note about the role result to reduce into a CasePatch.")


class ReportWriterInput(_SupervisorToolInput):
    report_instructions: str = Field(default="", description="Optional report-writing instructions from the user.")


class WriteCasePatchInput(_SupervisorToolInput):
    pass


CAPABILITY_CARDS = {
    "specialists": {
        "materials_advisor": "Generate material-gap tasks, explain rules/templates, and answer what to submit next. It may use RAG guidance internally.",
        "evidence_reviewer": "Extract/review/repair evidence. Use mode=extract for PDF/image/multi-attachment extraction, mode=review for evidence acceptance, and mode=repair for user corrections or superseding weak evidence.",
        "case_patch_writer": "Convert reviewer/advisor structured results into a CasePatch. It does not re-review evidence.",
        "report_writer": "Draft Chinese Markdown report content from case_state and evidence chain. It does not write files.",
    },
    "tools": {
        "read_case_state": "Read current case state snapshot.",
        "read_attachment": "Read current attachments with input={} or reopen a stored file with attachment_id/original_ref/name/path.",
        "list_case_files": "List case-local files.",
        "write_case_file": "Write report Markdown. Use input.content_ref=\"last_role:report_writer.markdown\" and optional input.relative_path only.",
        "render_pdf": "Render Markdown report to PDF. Use input={} after write_case_file; runtime fills timestamped markdown_path/pdf_path.",
        "write_case_patch": "Internal action after case_patch_writer only.",
    },
}


SPECIALIST_TOOL_DESCRIPTIONS = {
    "materials_advisor": CAPABILITY_CARDS["specialists"]["materials_advisor"],
    "evidence_reviewer": CAPABILITY_CARDS["specialists"]["evidence_reviewer"],
    "case_patch_writer": CAPABILITY_CARDS["specialists"]["case_patch_writer"],
    "report_writer": CAPABILITY_CARDS["specialists"]["report_writer"],
    "write_case_patch": "Persist the latest case_patch_writer result. Use only after case_patch_writer has produced a valid patch.",
}


SPECIALIST_TOOL_MODELS: dict[str, type[BaseModel]] = {
    "materials_advisor": MaterialsAdvisorInput,
    "evidence_reviewer": EvidenceReviewerInput,
    "case_patch_writer": CasePatchWriterInput,
    "report_writer": ReportWriterInput,
    "write_case_patch": WriteCasePatchInput,
}


def sorted_specialist_tool_specs() -> tuple[tuple[str, str, type[BaseModel]], ...]:
    return tuple(
        (
            name,
            SPECIALIST_TOOL_DESCRIPTIONS.get(name, ""),
            SPECIALIST_TOOL_MODELS[name],
        )
        for name in sorted(SPECIALIST_TOOL_MODELS)
    )
