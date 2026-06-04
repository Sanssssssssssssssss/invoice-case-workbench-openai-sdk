from __future__ import annotations

from app.config import Settings
from app.llm import LlmClient, _model_extra_body


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
