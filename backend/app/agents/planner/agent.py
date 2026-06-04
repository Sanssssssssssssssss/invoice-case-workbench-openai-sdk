from __future__ import annotations

from typing import Any

from app.llm import LlmClient
from app.runtime.supervisor_contract import CAPABILITY_CARDS, supervisor_openai_tools
from app.prompt_loader import load_system_prompt


class Planner:
    """Supervisor planner agent.

    The name stays `Planner` because the product and trace UI already call this
    role planner, but the contract is now SupervisorDecision rather than a
    backend route action.
    """

    def __init__(self, llm: LlmClient | None = None) -> None:
        self.llm = llm or LlmClient()
        self.prompt = load_system_prompt("agents/planner/prompt.md")

    def decide(
        self,
        *,
        user_message: str,
        planner_context: dict[str, Any],
        step_count: int,
        tool_cards: list[dict[str, Any]] | None = None,
    ) -> Any:
        capability_cards = dict(CAPABILITY_CARDS)
        capability_cards["tools"] = tool_cards if tool_cards is not None else CAPABILITY_CARDS["tools"]
        payload = {
            "user_message": user_message,
            "context_pack": planner_context,
            "step_count": step_count,
            "capability_cards": capability_cards,
        }
        return self.llm.complete_with_tools(
            role="planner",
            system_prompt=self.prompt,
            payload=payload,
            tools=supervisor_openai_tools(tool_cards or []),
            prompt_version="supervisor_planner_v1.2+policy_gate_v1.1",
        )
