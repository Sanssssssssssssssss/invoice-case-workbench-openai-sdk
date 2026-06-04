from __future__ import annotations

from typing import Any

from app.agents.capabilities import role_prompt_version
from app.llm import LlmClient
from app.prompt_loader import load_system_prompt
from app.state.schemas import CasePatch


SYSTEM_PROMPT = load_system_prompt("agents/patch_builder/prompt.md")


class CasePatchWriter:
    def __init__(self, llm: LlmClient | None = None) -> None:
        self.llm = llm or LlmClient()

    def run(
        self,
        *,
        role_result: dict[str, Any],
        case_state: dict[str, Any],
        user_message: str = "",
        memory_hints: list[dict[str, Any]] | None = None,
        supervisor_task: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "role_result": role_result,
            "case_state": case_state,
            "user_message": user_message,
            "memory_hints": memory_hints or [],
            "supervisor_task": supervisor_task or {},
        }
        return self.llm.complete_structured(
            role="case_patch_writer",
            system_prompt=SYSTEM_PROMPT,
            payload=payload,
            model_type=CasePatch,
            prompt_version=role_prompt_version("case_patch_writer"),
        ).model_dump()
