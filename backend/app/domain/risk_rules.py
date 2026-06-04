from __future__ import annotations

import json
from typing import Any


STRONG_CONFLICT_TERMS = (
    "mismatch",
    "conflict",
    "discrepancy",
    "duplicate found",
    "duplicate invoice found: yes",
    "potential duplicate payment",
    "prior payment found: yes",
    "clearing document found: yes",
    "amount mismatch",
    "bank mismatch",
    "supplier mismatch",
    "invoice number mismatch",
    "冲突",
    "不一致",
    "差异",
    "重复付款检查命中",
    "疑似重复付款",
    "潜在重复付款",
    "存在历史付款",
)

NO_CONFLICT_TERMS = (
    "no conflict",
    "no discrepancy",
    "no duplicate invoice found",
    "no duplicate found",
    "duplicate invoice found: no",
    "no prior payment found",
    "no clearing document exists",
    "prior payment found: no",
    "clearing document found: no",
    "未发现冲突",
    "无冲突",
    "未发现差异",
    "无差异",
    "未发现重复付款",
    "均未发现重复",
    "无未解决的重复付款冲突",
    "无数量冲突",
    "数量一致",
    "不存在历史付款",
    "税额未单列",
    "税额未单独列示",
    "reverse charge",
    "反向征收",
    "可作为po核对参考",
    "可作为后续po核对参考",
)


RESOLVED_CONFLICT_TERMS = (
    "resolved",
    "clarified",
    "superseded",
    "old conflict covered",
    "use pdf original",
    "冲突已澄清",
    "冲突已经澄清",
    "冲突已解决",
    "旧冲突已覆盖",
    "按用户要求澄清",
    "已澄清",
    "澄清",
    "已解决",
    "已覆盖",
    "以pdf原件为准",
    "以 pdf 原件为准",
    "以pdf为准",
    "ocr误识已澄清",
    "ocr噪声冲突已澄清",
)


UNRESOLVED_CONFLICT_TERMS = (
    "unresolved",
    "not resolved",
    "still unresolved",
    "待核对",
    "待确认",
    "需要核对",
    "需要确认",
    "仍需",
    "仍然存在",
    "未解决",
    "未澄清",
    "需要澄清",
)


def derived_conflicts(data: dict[str, Any]) -> list[str]:
    if data.get("conflicts"):
        return []
    text = _evidence_conflict_text(data)
    if not text:
        return []
    conflict_text = text.lower()
    if _looks_like_resolved_conflict_note(conflict_text):
        return []
    for term in NO_CONFLICT_TERMS:
        conflict_text = conflict_text.replace(term, " ")
    if not any(term in conflict_text for term in STRONG_CONFLICT_TERMS):
        return []
    summary = _first_non_empty_text(data.get("reviewer_notes"), data.get("summary"), data.get("content"))
    if len(summary) > 220:
        summary = summary[:217].rstrip() + "..."
    return [f"Derived conflict signal from reviewer output: {summary}"]


def resolved_conflict_note(value: Any) -> bool:
    return _looks_like_resolved_conflict_note(_jsonish_text(value).lower())


def _looks_like_resolved_conflict_note(text: str) -> bool:
    if not text:
        return False
    if any(term in text for term in UNRESOLVED_CONFLICT_TERMS):
        return False
    return any(term in text for term in RESOLVED_CONFLICT_TERMS)


def _evidence_conflict_text(data: dict[str, Any]) -> str:
    review_result = data.get("review_result") if isinstance(data.get("review_result"), dict) else {}
    pieces = [
        data.get("summary"),
        data.get("content"),
        data.get("reviewer_notes"),
        data.get("quoted_text"),
        review_result.get("reason"),
    ]
    return " ".join(_jsonish_text(piece) for piece in pieces if piece)


def _jsonish_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _first_non_empty_text(*values: Any) -> str:
    for value in values:
        text = _jsonish_text(value).strip() if value is not None else ""
        if text:
            return text
    return "reviewer output indicates an unresolved evidence conflict"
