from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.state.schemas import RequirementId


class _SupervisorToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MaterialsAdvisorInput(_SupervisorToolInput):
    question_focus: str = Field(default="", description="Optional short focus for the material/rule/template question.")


class EvidenceReviewerInput(_SupervisorToolInput):
    mode: Literal["review"] = Field(default="review", description="Run the Evidence Compiler review loop.")
    active_requirement_ids: list[RequirementId] = Field(
        default_factory=list,
        description="Actual Requirement ids selected from profile value lists; profile map keys are grouping labels and are invalid here. Invoice review always includes invoice_calculation_valid. Omit when the case already defines its scope.",
    )


class CasePatchWriterInput(_SupervisorToolInput):
    note: str = Field(default="", description="Optional note about the role result to reduce into a CasePatch.")


class ReportWriterInput(_SupervisorToolInput):
    pass


class WriteCasePatchInput(_SupervisorToolInput):
    pass


CAPABILITY_CARDS = {
    "specialists": {
        "materials_advisor": "Generate material-gap tasks, explain rules/templates, and answer what to submit next. It may use RAG guidance internally.",
        "evidence_reviewer": "Compile active requirements into a ProofPlan, inspect evidence in a read-only tool sandbox, and verify every atomic check. Pass exact active_requirement_ids for a new scope; mode is always review.",
        "case_patch_writer": "Convert reviewer/advisor structured results into a CasePatch. It does not re-review evidence.",
        "report_writer": "Draft Chinese Markdown from the canonical consumer packet and the user's request. It does not write files.",
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
