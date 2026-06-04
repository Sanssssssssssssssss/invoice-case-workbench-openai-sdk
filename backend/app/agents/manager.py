from __future__ import annotations

from typing import Any

from agents import Agent, FunctionTool, ModelSettings

from app.config import Settings
from app.prompt_loader import load_system_prompt


MANAGER_PROMPT = load_system_prompt("agents/planner/prompt.md")


class CaseManagerAgentFactory:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def build(self, tools: list[FunctionTool], *, metadata: dict[str, Any] | None = None) -> Agent:
        _ = metadata
        return Agent(
            name="case_manager",
            instructions=MANAGER_PROMPT,
            model=self.settings.llm_model,
            model_settings=ModelSettings(
                temperature=self._temperature(),
                extra_body=self._extra_body(),
            ),
            tools=tools,
        )

    def _temperature(self) -> float:
        model = self.settings.llm_model.lower()
        if model == "kimi-k2.5" and (self.settings.llm_thinking_type or "").lower() == "disabled":
            return 0.6
        if model.startswith("kimi-k2"):
            return 1.0
        return self.settings.llm_temperature

    def _extra_body(self) -> dict[str, Any] | None:
        if self.settings.llm_model.lower() == "kimi-k2.5":
            return {"thinking": {"type": str(self.settings.llm_thinking_type or "disabled")}}
        return None
