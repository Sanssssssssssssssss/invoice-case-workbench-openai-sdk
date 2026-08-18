from __future__ import annotations

from app.agents.manager import CaseManagerAgentFactory
from app.agents.registry import RoleRegistry
from app.agents.thinking import manager_tool_loop_thinking_type, role_thinking_type
from app.config import Settings
from app.llm import LlmClient, _model_extra_body
from app.runtime.agents_sdk import _use_responses_api


def test_kimi_agents_sdk_settings_keep_thinking_toggle() -> None:
    disabled = LlmClient(Settings(llm_model="kimi-k2.5", llm_temperature=0.1, llm_thinking_type="disabled"))
    enabled = LlmClient(Settings(llm_model="kimi-k2.5", llm_temperature=0.1, llm_thinking_type="enabled"))

    assert disabled._temperature("kimi-k2.5") == 0.6  # noqa: SLF001
    assert enabled._temperature("kimi-k2.5") == 1.0  # noqa: SLF001
    assert _model_extra_body("kimi-k2.5", "disabled") == {"thinking": {"type": "disabled"}}
    assert _model_extra_body("kimi-k2.5", "enabled") == {"thinking": {"type": "enabled"}}


def test_non_kimi_agents_sdk_settings_use_plain_model_temperature() -> None:
    client = LlmClient(Settings(llm_model="gpt-4.1-mini", llm_temperature=0.2, llm_thinking_type="enabled"))

    assert client._temperature("gpt-4.1-mini") == 0.2  # noqa: SLF001
    assert _model_extra_body("gpt-4.1-mini", "enabled") is None


def test_deepseek_v4_uses_the_official_responses_contract() -> None:
    assert _use_responses_api("deepseek", "https://api.deepseek.com") is True
    assert _model_extra_body("deepseek-v4-flash", "disabled") == {"reasoning": {"effort": "none"}}
    assert _model_extra_body("deepseek-v4-flash", "enabled") == {"reasoning": {"effort": "high"}}


def test_role_thinking_scope_is_limited_to_planner() -> None:
    configured = "enabled"

    assert role_thinking_type("planner", {}, configured) == "enabled"
    assert role_thinking_type("task_compiler", {}, configured) == "disabled"
    assert role_thinking_type("executor", {}, configured) == "disabled"
    assert role_thinking_type("fine_verifier", {}, configured) == "disabled"
    assert role_thinking_type("materials_advisor", {}, configured) == "disabled"
    assert role_thinking_type("case_patch_writer", {}, configured) == "disabled"
    assert role_thinking_type("report_writer", {}, configured) == "disabled"
    assert role_thinking_type("summarizer", {}, configured) == "disabled"


def test_manager_tool_loop_and_specialists_disable_kimi_thinking() -> None:
    settings = Settings(llm_model="kimi-k2.5", llm_temperature=0.1, llm_thinking_type="enabled")
    manager = CaseManagerAgentFactory(settings).build([])
    registry = RoleRegistry(LlmClient(settings))

    assert manager_tool_loop_thinking_type("kimi-k2.5", "enabled") == "disabled"
    assert manager.model_settings.extra_body == {"thinking": {"type": "disabled"}}
    assert registry.agent("materials_advisor").model_settings.extra_body == {"thinking": {"type": "disabled"}}
    assert "evidence_reviewer" not in registry.role_names
