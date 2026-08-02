from __future__ import annotations

import json
from typing import Any


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


def resolved_conflict_note(value: Any) -> bool:
    text = _jsonish_text(value).lower()
    return bool(text) and not any(term in text for term in UNRESOLVED_CONFLICT_TERMS) and any(
        term in text for term in RESOLVED_CONFLICT_TERMS
    )


def _jsonish_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if hasattr(value, "model_dump"):
        value = value.model_dump(exclude_none=True)
    return json.dumps(value, ensure_ascii=False, default=str)
