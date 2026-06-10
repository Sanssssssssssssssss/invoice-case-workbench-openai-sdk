from __future__ import annotations

from typing import Any

from agents import Agent, FunctionTool, ModelSettings

from app.agents.thinking import manager_tool_loop_thinking_type, model_extra_body_for_thinking, temperature_for_thinking
from app.config import Settings
from app.prompt_loader import load_system_prompt
from app.runtime.context_partition import prompt_cache_model_settings_kwargs


MANAGER_PROMPT = load_system_prompt("agents/planner/prompt.md")


class CaseManagerAgentFactory:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def build(self, tools: list[FunctionTool], *, metadata: dict[str, Any] | None = None) -> Agent:
        metadata = metadata or {}
        prompt_partition = metadata.get("prompt_partition") if isinstance(metadata.get("prompt_partition"), dict) else {}
        return Agent(
            name="case_manager",
            instructions=MANAGER_PROMPT,
            model=self.settings.llm_model,
            model_settings=ModelSettings(
                temperature=self._temperature(),
                extra_body=self._extra_body(),
                **prompt_cache_model_settings_kwargs(self.settings, prompt_partition),
            ),
            tools=tools,
        )

    def _temperature(self) -> float:
        return temperature_for_thinking(
            self.settings.llm_model,
            self.settings.llm_temperature,
            manager_tool_loop_thinking_type(self.settings.llm_model, self.settings.llm_thinking_type),
        )

    def _extra_body(self) -> dict[str, Any] | None:
        return model_extra_body_for_thinking(
            self.settings.llm_model,
            manager_tool_loop_thinking_type(self.settings.llm_model, self.settings.llm_thinking_type),
        )
