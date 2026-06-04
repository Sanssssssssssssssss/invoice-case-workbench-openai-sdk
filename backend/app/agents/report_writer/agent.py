from __future__ import annotations

from typing import Any

from app.agents.capabilities import role_prompt_version
from app.llm import LlmClient
from app.prompt_loader import load_prompt, load_system_prompt
from app.state.schemas import ReportWriterResult


SYSTEM_PROMPT = (
    load_system_prompt("agents/report_writer/prompt.md").rstrip()
    + "\n\n---\n\n"
    + load_prompt("agents/report_writer/pdf_skill.md").lstrip()
)


class ReportWriter:
    def __init__(self, llm: LlmClient | None = None) -> None:
        self.llm = llm or LlmClient()

    def run(
        self,
        *,
        case_state: dict[str, Any],
        evidence: list[dict[str, Any]] | None = None,
        conversation_summary: str = "",
        rag_context: list[dict[str, Any]] | None = None,
        user_request: str = "",
        report_instructions: str = "",
        attachment_manifest: dict[str, Any] | None = None,
        evidence_chain_context: dict[str, Any] | None = None,
        memory_hints: list[dict[str, Any]] | None = None,
        supervisor_task: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "case_state": case_state,
            "evidence": evidence or [],
            "conversation_summary": conversation_summary,
            "rag_context": rag_context or [],
            "user_request": user_request,
            "report_instructions": report_instructions,
            "attachment_manifest": attachment_manifest or {},
            "evidence_chain_context": evidence_chain_context or {},
            "memory_hints": memory_hints or [],
            "supervisor_task": supervisor_task or {},
        }
        return self.llm.complete_structured(
            role="report_writer",
            system_prompt=SYSTEM_PROMPT,
            payload=payload,
            model_type=ReportWriterResult,
            prompt_version=role_prompt_version("report_writer"),
        ).model_dump()
