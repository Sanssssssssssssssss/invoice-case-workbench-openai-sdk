from __future__ import annotations

from typing import Any


def sdk_run_summary(result: Any) -> dict[str, Any]:
    """Return a small, redacted summary of an Agents SDK run result."""
    return {
        "final_output_preview": str(getattr(result, "final_output", "") or "")[:1200],
        "new_item_types": [type(item).__name__ for item in getattr(result, "new_items", []) or []],
        "raw_response_count": len(getattr(result, "raw_responses", []) or []),
        "interruption_count": len(getattr(result, "interruptions", []) or []),
        "last_response_id": _safe_call(result, "last_response_id"),
        "last_agent": getattr(getattr(result, "last_agent", None), "name", ""),
    }


def sdk_usage_summary(result: Any) -> dict[str, int]:
    totals = {"input": 0, "output": 0, "total": 0}
    for response in getattr(result, "raw_responses", []) or []:
        usage = getattr(response, "usage", None)
        if hasattr(usage, "model_dump"):
            usage = usage.model_dump()
        if not isinstance(usage, dict):
            continue
        totals["input"] += int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0)
        totals["output"] += int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0)
        totals["total"] += int(usage.get("total_tokens", 0) or 0)
    return {key: value for key, value in totals.items() if value}


def _safe_call(value: Any, name: str) -> Any:
    attr = getattr(value, name, None)
    if callable(attr):
        try:
            return attr()
        except Exception:
            return ""
    return attr or ""
