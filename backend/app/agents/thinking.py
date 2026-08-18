from __future__ import annotations

from typing import Any


def planner_thinking_type(configured: str | None) -> str:
    return _normalized_thinking_type(configured)


def manager_tool_loop_thinking_type(model_name: str, configured: str | None) -> str:
    if _requires_reasoning_content_tool_history(model_name):
        return "disabled"
    return planner_thinking_type(configured)


def role_thinking_type(role: str, payload: dict[str, Any] | None, configured: str | None) -> str:
    _ = payload
    normalized_role = str(role or "").lower()
    if normalized_role == "planner":
        return _normalized_thinking_type(configured)
    return "disabled"


def temperature_for_thinking(model_name: str, default: float, thinking_type: str | None) -> float:
    model = str(model_name or "").lower()
    if model == "kimi-k2.5" and str(thinking_type or "").lower() == "disabled":
        return 0.6
    if model.startswith("kimi-k2"):
        return 1.0
    return default


def model_extra_body_for_thinking(model_name: str, thinking_type: str | None) -> dict[str, Any] | None:
    model = str(model_name or "").lower()
    if model == "kimi-k2.5":
        return {"thinking": {"type": _normalized_thinking_type(thinking_type)}}
    if model.startswith("deepseek-v4-"):
        mode = _normalized_thinking_type(thinking_type)
        effort = "none" if mode in {"disabled", "none"} else mode if mode in {"low", "high", "max"} else "high"
        return {"reasoning": {"effort": effort}}
    return None


def _normalized_thinking_type(value: str | None) -> str:
    return str(value or "disabled").strip().lower() or "disabled"


def _requires_reasoning_content_tool_history(model_name: str) -> bool:
    model = str(model_name or "").strip().lower()
    return model == "kimi-k2.5"
