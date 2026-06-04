from __future__ import annotations

from typing import Any

from app.agents.capabilities import role_prompt_version
from app.llm import LlmClient
from app.prompt_loader import load_prompt, load_system_prompt
from app.state.schemas import MaterialsAdvisorResult


SYSTEM_PROMPT = "\n\n---\n\n".join(
    [
        load_system_prompt("agents/materials_advisor/prompt.md").rstrip(),
        load_prompt("agents/materials_advisor/task_skill.md").lstrip(),
    ]
)


class MaterialsAdvisor:
    def __init__(self, llm: LlmClient | None = None) -> None:
        self.llm = llm or LlmClient()

    def run(
        self,
        *,
        user_question: str,
        case_state: dict[str, Any],
        rag_context: list[dict[str, Any]] | None = None,
        attachment_manifest: dict[str, Any] | None = None,
        memory_hints: list[dict[str, Any]] | None = None,
        supervisor_task: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "user_question": user_question,
            "case_state": case_state,
            "rag_context": rag_context or [],
            "attachment_manifest": attachment_manifest or {},
            "memory_hints": memory_hints or [],
            "supervisor_task": supervisor_task or {},
        }
        return self.llm.complete_structured(
            role="materials_advisor",
            system_prompt=SYSTEM_PROMPT,
            payload=payload,
            model_type=MaterialsAdvisorResult,
            prompt_version=role_prompt_version("materials_advisor"),
        ).model_dump()
