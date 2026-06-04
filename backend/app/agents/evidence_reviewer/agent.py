from __future__ import annotations

from typing import Any

from app.agents.capabilities import role_prompt_version
from app.llm import LlmClient
from app.prompt_loader import load_system_prompt
from app.state.schemas import EvidenceReviewResult


SYSTEM_PROMPT = "\n\n".join(
    [
        load_system_prompt("agents/evidence_reviewer/prompt.md"),
        load_system_prompt("agents/evidence_reviewer/review_skill.md"),
        load_system_prompt("agents/evidence_reviewer/pdf_image_skill.md"),
        load_system_prompt("agents/evidence_reviewer/attachment_review_skill.md"),
    ]
)


class EvidenceReviewer:
    def __init__(self, llm: LlmClient | None = None) -> None:
        self.llm = llm or LlmClient()

    def run(
        self,
        *,
        mode: str = "review",
        user_message: str,
        case_state: dict[str, Any],
        rag_context: list[dict[str, Any]] | None = None,
        attachment_context: list[dict[str, Any]] | None = None,
        attachment_manifest: dict[str, Any] | None = None,
        extraction_context: list[dict[str, Any]] | None = None,
        extraction_result: dict[str, Any] | None = None,
        target_evidence_id: str = "",
        target_attachment_id: str = "",
        user_correction: str = "",
        memory_hints: list[dict[str, Any]] | None = None,
        supervisor_task: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        mode = mode if mode in {"extract", "review", "repair"} else "review"
        attachment_context = attachment_context or []
        payload = {
            "mode": mode,
            "user_message": user_message,
            "case_state": case_state,
            "rag_context": rag_context or [],
            "attachment_context": attachment_context,
            "attachment_manifest": attachment_manifest or {},
            "extraction_context": extraction_context or [],
            "extraction_result": extraction_result or {},
            "target_evidence_id": target_evidence_id,
            "target_attachment_id": target_attachment_id,
            "user_correction": user_correction,
            "memory_hints": memory_hints or [],
            "supervisor_task": supervisor_task or {},
        }
        return self.llm.complete_structured(
            role="evidence_reviewer",
            system_prompt=SYSTEM_PROMPT,
            payload=payload,
            model_type=EvidenceReviewResult,
            prompt_version=role_prompt_version("evidence_reviewer"),
        ).model_dump()
