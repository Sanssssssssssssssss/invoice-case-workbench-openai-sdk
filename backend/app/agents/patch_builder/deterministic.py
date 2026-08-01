from typing import Any

from app.state.schemas import CasePatch, EvidenceReviewResult


def reduce_review_to_patch(review: dict[str, Any]) -> dict[str, Any]:
    result = EvidenceReviewResult.model_validate(review)
    updates = result.suggested_patch.model_copy(deep=True)
    if not updates.evidence_cards and result.evidence_cards:
        updates.evidence_cards = result.evidence_cards
    if not updates.reply_brief and result.reply_to_user:
        updates.reply_brief = result.reply_to_user

    data = updates.model_dump(exclude_none=True)
    has_evidence = bool(data.get("add_evidence") or data.get("evidence_items"))
    has_updates = any(value not in (None, "", [], {}) for value in data.values())
    patch_type = "add_evidence" if has_evidence else "update_case" if has_updates else "no_change"
    return CasePatch(
        patch_type=patch_type,
        case_updates=updates,
        audit_note="Reduced deterministically from evidence_reviewer.suggested_patch.",
    ).model_dump(mode="json")


__all__ = ["reduce_review_to_patch"]
