from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.tools.rag_search import RagSkill


@dataclass
class RagGuidance:
    evidences: list[dict[str, Any]]
    debug: dict[str, Any]


def advisor_guidance(
    user_question: str,
    case_state: dict[str, Any],
    attachment_manifest: dict[str, Any],
    *,
    top_k: int = 5,
) -> RagGuidance:
    query = _advisor_guidance_query(user_question, case_state, attachment_manifest)
    return _retrieve(query, role="materials_advisor", top_k=top_k)


def _retrieve(query: str, *, role: str, top_k: int) -> RagGuidance:
    if not query:
        return RagGuidance([], {"role": role, "status": "skipped", "query": "", "source_ids": [], "top_k": top_k})
    try:
        result = RagSkill().retrieve(query=query, intent="policy_qa", top_k=top_k)
        evidences = [item.model_dump() for item in result.evidences]
        return RagGuidance(
            evidences,
            {
                "role": role,
                "status": "success",
                "query": query,
                "source_ids": [str(item.get("source_id") or "") for item in evidences],
                "source_paths": [str(item.get("source_path") or "") for item in evidences],
                "locators": [str(item.get("locator") or "") for item in evidences],
                "profile_ids": [str((item.get("fields") or {}).get("profile_id") or "") for item in evidences],
                "snippets": [_snippet_preview(str(item.get("snippet") or "")) for item in evidences],
                "scores": [float(item.get("score") or 0.0) for item in evidences],
                "channels": [str(item.get("channel") or "") for item in evidences],
                "top_k": top_k,
                "evidence_count": len(evidences),
            },
        )
    except Exception as exc:
        return RagGuidance(
            [],
            {
                "role": role,
                "status": "error",
                "query": query,
                "source_ids": [],
                "top_k": top_k,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )


def _advisor_guidance_query(user_question: str, case_state: dict[str, Any], attachment_manifest: dict[str, Any]) -> str:
    requirements = case_state.get("requirements") if isinstance(case_state, dict) else []
    requirement_terms: list[str] = []
    if isinstance(requirements, list):
        for item in requirements[:20]:
            if isinstance(item, dict):
                requirement_terms.append(" ".join(str(item.get(key) or "") for key in ("id", "label", "status", "guidance")))
    rows = attachment_manifest.get("attachments") if isinstance(attachment_manifest, dict) else []
    attachment_terms: list[str] = []
    if isinstance(rows, list):
        for item in rows[:8]:
            if isinstance(item, dict):
                attachment_terms.append(" ".join(str(item.get(key) or "") for key in ("name", "content_kind", "status", "summary")))
    text = "\n".join([str(user_question or ""), *requirement_terms, *attachment_terms]).strip()
    if not text:
        return ""
    return (
        "invoice review material requirements advisor guidance "
        f"{text[:1200]}"
    )


def _snippet_preview(value: str, max_chars: int = 360) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."
