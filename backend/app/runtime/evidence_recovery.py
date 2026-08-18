from __future__ import annotations

import re
from typing import Any

from app.domain.invoice_requirements import INVOICE_FIELD_REQUIREMENTS
from app.state.schemas import EvidenceReviewResult


EVIDENCE_REQUIREMENTS = {
    "invoice": ("invoice", *INVOICE_FIELD_REQUIREMENTS),
    "purchase_order": ("purchase_order", "po_number", "po_ref", "po_amount", "po_date"),
    "goods_receipt": (
        "goods_receipt",
        "goods_receipt_or_service_acceptance",
        "grn_number",
        "received_quantity",
        "inspection_status",
    ),
    "vendor_record": ("vendor_record", "vendor_identity", "vendor_status", "vendor_id", "bank_last_four"),
    "duplicate_payment_check": (
        "duplicate_payment_check",
        "duplicate_payment_screen",
        "duplicate_invoice_found",
        "prior_payment_found",
        "clearing_document",
        "payment_reference",
    ),
}
CANONICAL_REQUIREMENTS = {
    "goods_receipt": "goods_receipt_or_service_acceptance",
    "vendor_record": "vendor_identity",
    "duplicate_payment_check": "duplicate_payment_screen",
}


def materialize_reviewer_output(output: dict[str, Any], role_input: dict[str, Any]) -> dict[str, Any]:
    """Bind sparse model output to Runtime-owned attachment and patch fields."""

    if "suggested_patch" in output:  # compatibility for stored tests and old traces
        return EvidenceReviewResult.model_validate(output).model_dump(mode="json")
    mode = str(output.get("mode") or "review")
    if mode == "extract":
        return EvidenceReviewResult(
            mode="extract",
            extracted_fields=output.get("extracted_fields") or {},
            reply_to_user=str(output.get("reply_to_user") or ""),
        ).model_dump(mode="json")

    attachments = {
        str(item.get("attachment_id") or ""): item
        for item in role_input.get("attachment_context") or []
        if isinstance(item, dict) and item.get("attachment_id")
    }
    target_evidence_id, target_attachment_id = _repair_target(role_input) if mode == "repair" else ("", "")
    repair_applied = False
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in output.get("sources") or []:
        attachment_id = str(source.get("attachment_id") or "")
        if not attachment_id or attachment_id in seen or attachment_id not in attachments:
            raise ValueError(f"Reviewer source is not uniquely bound to attachment_context: {attachment_id or '(missing)'}")
        seen.add(attachment_id)
        attachment = attachments[attachment_id]
        text = _item_text(attachment)
        classification = str(source.get("classification") or "")
        should_accept = bool(source.get("should_accept")) and classification == "business_evidence"
        evidence_type = str(source.get("type") or "unknown")
        direct_supports = _direct_supports(
            evidence_type,
            source.get("supports") or [],
            role_input.get("active_requirement_contracts") or [],
        )
        quotes = _source_quotes({**source, "supports": direct_supports})
        metadata = {
            "classification": classification,
            "attachment_id": attachment_id,
            "original_ref": attachment.get("original_ref", ""),
            "extraction_ref": attachment.get("extraction_ref", ""),
            "source_filename": attachment.get("name", ""),
            "content_kind": attachment.get("content_kind", ""),
            "extraction_method": attachment.get("extraction_method", ""),
            "extracted_fields": _extracted_fields(attachment, text),
        }
        if (
            target_evidence_id
            and not repair_applied
            and (not target_attachment_id or target_attachment_id == attachment_id)
        ):
            metadata.update(
                review_stage="corrected",
                supersedes_evidence_id=target_evidence_id,
            )
            repair_applied = True
        items.append(
            {
                "type": evidence_type,
                "reference": str(attachment.get("original_ref") or attachment.get("name") or ""),
                "credibility": source.get("credibility") or "medium",
                "summary": _compact_text(source.get("summary") or text, 260),
                "source": "attachment",
                "content": _compact_text(" ".join(quotes) or text, 420),
                "review_result": {
                    "mode": mode,
                    "should_accept": should_accept,
                    "source_traceability": source.get("source_traceability") or "unclear",
                },
                "supports": direct_supports,
                "conflicts": source.get("conflicts") or [],
                "quoted_text": quotes,
                "reviewer_notes": _compact_text(source.get("reviewer_notes") or "", 420),
                "metadata": metadata,
                "local_source_handle": source.get("local_source_handle") or f"s{len(items) + 1}",
                "semantic_claims": source.get("semantic_claims") or [],
                "semantic_proposals": source.get("semantic_proposals") or [],
            }
        )
    patch = {
        "add_evidence": items,
        "risk_flags": list(output.get("risk_flags") or []),
        "next_questions": list(output.get("next_questions") or []),
        "reply_brief": str(output.get("reply_to_user") or ""),
    }
    return EvidenceReviewResult(
        mode=mode,
        source_doc_id="attachment_batch",
        evidence_type="unknown",
        credibility="high" if items and all(item["credibility"] == "high" for item in items) else "medium",
        source_traceability="unclear",
        risk_flags=patch["risk_flags"],
        should_accept=any(item["review_result"]["should_accept"] for item in items),
        suggested_patch=patch,
        reply_to_user=patch["reply_brief"],
    ).model_dump(mode="json")


def _repair_target(role_input: dict[str, Any]) -> tuple[str, str]:
    evidence_id = str(role_input.get("target_evidence_id") or "").strip()
    attachment_id = str(role_input.get("target_attachment_id") or "").strip()
    rows = (role_input.get("case_state") or {}).get("evidence_items") or []
    if evidence_id and not attachment_id:
        matches = [
            str((item.get("metadata") or {}).get("attachment_id") or "")
            for item in rows
            if isinstance(item, dict) and str(item.get("id") or "") == evidence_id
        ]
        attachment_id = matches[0] if len(matches) == 1 else ""
    if attachment_id and not evidence_id:
        matches = [
            str(item.get("id") or "")
            for item in rows
            if isinstance(item, dict)
            and str((item.get("metadata") or {}).get("attachment_id") or "") == attachment_id
        ]
        evidence_id = matches[0] if len(matches) == 1 else ""
    return evidence_id, attachment_id


def _source_quotes(source: dict[str, Any]) -> list[str]:
    values = [
        *(str(item.get("quoted_text") or "") for item in source.get("supports") or [] if isinstance(item, dict)),
        *(str(item.get("source_quote") or "") for item in source.get("semantic_claims") or [] if isinstance(item, dict)),
    ]
    return [_compact_text(value, 360) for value in dict.fromkeys(values) if value.strip()]


def _direct_supports(
    evidence_type: str,
    supports: list[dict[str, Any]],
    active_contracts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    typed = {
        CANONICAL_REQUIREMENTS.get(requirement, requirement)
        for requirement in EVIDENCE_REQUIREMENTS.get(evidence_type, ())
    }
    active = {
        str(item.get("requirement_id") or "")
        for item in active_contracts
        if isinstance(item, dict) and item.get("proof_template") == "evidence_support"
    }
    known_typed = {
        CANONICAL_REQUIREMENTS.get(requirement, requirement)
        for requirements in EVIDENCE_REQUIREMENTS.values()
        for requirement in requirements
    }
    allowed = (active - known_typed) if evidence_type == "unknown" else typed
    if active:
        allowed &= active
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for support in supports:
        raw_requirement = str(support.get("requirement") or "")
        requirement = CANONICAL_REQUIREMENTS.get(raw_requirement, raw_requirement)
        if requirement not in allowed or requirement in seen:
            continue
        seen.add(requirement)
        result.append({**support, "requirement": requirement})
    return result


def _item_text(item: dict[str, Any]) -> str:
    return " ".join(str(item.get("body_markdown") or item.get("content") or "").split())


def _extracted_fields(item: dict[str, Any], text: str) -> dict[str, dict[str, Any]]:
    fields: dict[str, dict[str, Any]] = {}
    for row in item.get("field_inventory") or []:
        if not isinstance(row, dict):
            continue
        field = str(row.get("field") or "").strip()
        value = row.get("value")
        if not field or value in (None, "", [], {}):
            continue
        fields[field] = {
            "value": value,
            "status": str(row.get("status") or "present"),
            "source_quote": str(row.get("source_quote") or value),
            "source_locator": str(row.get("locator") or "text_direct"),
            "confidence": _confidence(row.get("confidence")),
        }
    regex_fields = {
        "invoice_number": r"\bINV-\d+\b",
        "po_ref": r"\bPO-\d+\b",
        "grn_ref": r"\bGRN-\d+\b",
        "vendor_id": r"\bV-[A-Z0-9-]+\b",
        "prior_payment_doc": r"\bPAY-\d{4}-\d+\b",
        "clearing_doc": r"\bCLR-\d{4}-\d+\b",
    }
    for field, pattern in regex_fields.items():
        match = re.search(pattern, text, flags=re.I)
        if match and field not in fields:
            fields[field] = _field(match.group(0), match.group(0), "text_regex", "high")
    supplier = _line_value(text, ("Supplier legal name", "Search supplier", "Supplier"))
    if supplier and "supplier" not in fields:
        fields["supplier"] = _field(supplier, supplier, "text_line", "high")
    amount = _line_value(text, ("Invoice total amount", "PO total amount", "Search amount", "Total amount"))
    if amount and "amount_total" not in fields:
        fields["amount_total"] = _field(amount, amount, "text_line", "high")
    currency = _currency(text)
    if currency and "currency_tax" not in fields:
        fields["currency_tax"] = _field(currency, currency, "text_regex", "medium")
    return fields


def _field(value: Any, quote: str, locator: str, confidence: str) -> dict[str, Any]:
    return {
        "value": value,
        "status": "present",
        "source_quote": quote,
        "source_locator": locator,
        "confidence": _confidence(confidence),
    }


def _confidence(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in {"low", "medium", "high"} else "medium"


def _line_value(text: str, labels: tuple[str, ...]) -> str:
    for label in labels:
        match = re.search(rf"{re.escape(label)}\s*:\s*([^\n\r\-|]+)", text, flags=re.I)
        if match:
            return match.group(1).strip(" .;")
    return ""


def _currency(text: str) -> str:
    match = re.search(r"\b(CNY|USD|EUR|GBP|JPY|RMB)\b", text, flags=re.I)
    return match.group(1).upper() if match else ""


def _compact_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 14)].rstrip() + "...[truncated]"
