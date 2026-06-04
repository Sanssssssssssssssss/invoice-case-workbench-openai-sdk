from __future__ import annotations

import re
from typing import Any

from app.domain.invoice_requirements import AP_LITE_REQUIREMENTS, requirement_label
from app.state.schemas import EvidenceReviewResult


TEXT_DIRECT_METHODS = {"text_direct", "markdown_direct", "csv_direct"}
EVIDENCE_REQUIREMENTS = {
    "invoice": ("invoice",),
    "purchase_order": ("purchase_order",),
    "goods_receipt": ("goods_receipt", "goods_receipt_or_service_acceptance"),
    "vendor_record": ("vendor_record", "vendor_identity"),
    "duplicate_payment_check": ("duplicate_payment_check", "duplicate_payment_screen"),
}
CANONICAL_REQUIREMENTS = {
    "goods_receipt": "goods_receipt_or_service_acceptance",
    "vendor_record": "vendor_identity",
    "duplicate_payment_check": "duplicate_payment_screen",
}


def recover_text_direct_review(attachment_batch: dict[str, Any], *, error: Exception | None = None) -> dict[str, Any] | None:
    attachments = _successful_text_attachments(attachment_batch)
    if not attachments:
        return None
    items: list[dict[str, Any]] = []
    for attachment in attachments:
        item = _evidence_item(attachment)
        if item:
            items.append(item)
            continue
        if _is_non_business_context_attachment(attachment):
            continue
        return None
    if not items:
        return None
    duplicate = _duplicate_facts(attachments)
    risk_flags = _risk_flags(duplicate)
    for item in items:
        if item["type"] == "duplicate_payment_check" and risk_flags:
            conflict = _duplicate_conflict_text(duplicate)
            item["conflicts"] = [conflict]
            item["reviewer_notes"] = conflict
            item["review_result"]["risk_flags"] = risk_flags
            item["metadata"]["duplicate_payment_facts"] = duplicate
    patch = {
        "summary": _case_summary(items, duplicate),
        "case_profile": _case_profile(items, duplicate),
        "requirements": _requirements_for_items(items),
        "add_evidence": items,
        "risk_flags": risk_flags,
        "next_questions": _next_questions(duplicate),
        "next_action_hint": "final_answer",
        "reply_brief": _reply_brief(duplicate),
        "evidence_cards": [_evidence_card(item) for item in items],
    }
    result = {
        "mode": "review",
        "source_doc_id": "attachment_batch",
        "evidence_type": "process_log",
        "credibility": "high",
        "extracted_fields": _merged_extracted_fields(items, duplicate),
        "source_traceability": "system_export",
        "support_level": "partial" if duplicate else "full",
        "risk_flags": risk_flags,
        "should_accept": True,
        "reason": _reason(error),
        "supports": _top_level_supports(items),
        "conflicts": _top_level_conflicts(duplicate),
        "evidence_cards": patch["evidence_cards"],
        "suggested_patch": patch,
        "reply_to_user": _reply_brief(duplicate),
    }
    return EvidenceReviewResult.model_validate(result).model_dump(mode="json")


def _successful_text_attachments(attachment_batch: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(attachment_batch, dict):
        return []
    attachments = [item for item in attachment_batch.get("attachments") or [] if isinstance(item, dict)]
    if not attachments:
        return []
    result: list[dict[str, Any]] = []
    for item in attachments:
        if item.get("status") != "success":
            return []
        content_kind = str(item.get("content_kind") or "").lower()
        methods = {str(value or "").lower() for value in item.get("extraction_methods") or []}
        method = str(item.get("extraction_method") or "").lower()
        if content_kind != "text" and method not in TEXT_DIRECT_METHODS and not methods.intersection(TEXT_DIRECT_METHODS):
            return []
        if not str(item.get("body_markdown") or item.get("content") or "").strip():
            return []
        result.append(item)
    return result


def _evidence_item(item: dict[str, Any]) -> dict[str, Any]:
    evidence_type = _evidence_type(item)
    if evidence_type not in EVIDENCE_REQUIREMENTS:
        return {}
    text = _item_text(item)
    extracted_fields = _extracted_fields(item, text)
    supports = [
        {
            "requirement": requirement,
            "support_level": "full",
            "quoted_text": _quote_for_requirement(requirement, item, text),
        }
        for requirement in EVIDENCE_REQUIREMENTS[evidence_type]
    ]
    return {
        "type": evidence_type,
        "reference": str(item.get("original_ref") or item.get("name") or ""),
        "credibility": "high",
        "summary": _summary_for_item(evidence_type, item, text),
        "source": "attachment",
        "content": _compact_text(text, 420),
        "review_result": {
            "mode": "review",
            "should_accept": True,
            "source_traceability": "system_export",
            "runtime_recovery": "text_direct_review_after_specialist_timeout",
        },
        "supports": supports,
        "conflicts": [],
        "quoted_text": _quoted_text(item, text),
        "reviewer_notes": "Recovered by runtime text-direct review after specialist timeout.",
        "metadata": {
            "attachment_id": item.get("attachment_id", ""),
            "name": item.get("name", ""),
            "original_ref": item.get("original_ref", ""),
            "extraction_ref": item.get("extraction_ref", ""),
            "content_kind": item.get("content_kind", ""),
            "extraction_method": item.get("extraction_method", ""),
            "runtime_recovery": "text_direct_review_after_specialist_timeout",
            "review_stage": "reviewed",
            "source_traceability": "system_export",
            "requirement_aliases": list(EVIDENCE_REQUIREMENTS[evidence_type]),
            "extracted_fields": extracted_fields,
            "field_inventory": _compact_field_inventory(item.get("field_inventory")),
            "quality_notes": list(item.get("quality_notes") or [])[:6],
        },
    }


def _evidence_type(item: dict[str, Any]) -> str:
    text = _item_text(item)
    match = re.search(r"Evidence\s+type:\s*([a-z_]+)", text, flags=re.I)
    if match:
        return _normalize_evidence_type(match.group(1))
    name = str(item.get("name") or "").lower()
    if "purchase_order" in name or "po-" in name:
        return "purchase_order"
    if "goods_receipt" in name or "grn-" in name:
        return "goods_receipt"
    if "vendor" in name:
        return "vendor_record"
    if "duplicate" in name:
        return "duplicate_payment_check"
    if "invoice" in name or "inv-" in name:
        return "invoice"
    return "unknown"


def _is_non_business_context_attachment(item: dict[str, Any]) -> bool:
    name = str(item.get("name") or "").strip().lower()
    if name in {"readme.md", "readme.txt", "instructions.md", "notes.md"}:
        return True
    text = _item_text(item).lower()
    if name.startswith("readme") and "expected high-level result" in text:
        return True
    if "sample" in text and "suggested desktop test" in text:
        return True
    return False


def _normalize_evidence_type(value: str) -> str:
    value = str(value or "").strip().lower()
    if value in {"goods_receipt_or_service_acceptance", "goods_receipt_note"}:
        return "goods_receipt"
    if value == "vendor_identity":
        return "vendor_record"
    if value == "duplicate_payment_screen":
        return "duplicate_payment_check"
    return value


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


def _duplicate_facts(attachments: list[dict[str, Any]]) -> dict[str, Any]:
    text = " ".join(_item_text(item) for item in attachments)
    prior_payment = _first(r"\bPAY-\d{4}-\d+\b", text)
    clearing_doc = _first(r"\bCLR-\d{4}-\d+\b", text)
    duplicate_yes = bool(re.search(r"Duplicate invoice found:\s*Yes|Prior payment found:\s*Yes|Potential duplicate", text, re.I))
    if not duplicate_yes and not prior_payment and not clearing_doc:
        return {}
    return {
        "duplicate_payment_found": duplicate_yes or bool(prior_payment),
        "prior_payment_doc": prior_payment,
        "clearing_doc": clearing_doc,
        "source_quote": _duplicate_quote(text),
    }


def _first(pattern: str, text: str) -> str:
    match = re.search(pattern, text, flags=re.I)
    return match.group(0) if match else ""


def _duplicate_quote(text: str) -> str:
    match = re.search(r"Duplicate invoice found:[^.]+?(?:reconciled\.|$)", text, flags=re.I)
    if match:
        return _compact_text(match.group(0), 360)
    return _compact_text(text, 360)


def _risk_flags(duplicate: dict[str, Any]) -> list[str]:
    if not duplicate:
        return []
    refs = " ".join(str(duplicate.get(key) or "") for key in ("prior_payment_doc", "clearing_doc")).strip()
    return [f"duplicate_payment_risk: prior payment or clearing record found {refs}".strip()]


def _duplicate_conflict_text(duplicate: dict[str, Any]) -> str:
    prior = duplicate.get("prior_payment_doc") or "prior payment"
    clearing = duplicate.get("clearing_doc") or "clearing document"
    return f"duplicate_payment_screen conflict: historical payment {prior} and clearing document {clearing} require reconciliation."


def _requirements_for_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ids: list[str] = []
    for item in items:
        for support in item.get("supports") or []:
            requirement = str(support.get("requirement") or "")
            requirement = CANONICAL_REQUIREMENTS.get(requirement, requirement)
            if requirement and requirement not in ids:
                ids.append(requirement)
    ordered = [item for item in AP_LITE_REQUIREMENTS if item in ids]
    ordered.extend(item for item in ids if item not in ordered)
    return [
        {
            "id": requirement_id,
            "label": requirement_label(requirement_id),
            "kind": _requirement_kind(requirement_id),
            "required": True,
        }
        for requirement_id in ordered
    ]


def _requirement_kind(requirement_id: str) -> str:
    if requirement_id in {"duplicate_payment_check", "duplicate_payment_screen"}:
        return "risk_check"
    return "document"


def _top_level_supports(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    supports: list[dict[str, Any]] = []
    for item in items:
        supports.extend(item.get("supports") or [])
    return supports[:12]


def _top_level_conflicts(duplicate: dict[str, Any]) -> list[dict[str, Any]]:
    if not duplicate:
        return []
    return [
        {
            "type": "risk",
            "conflict_type": "duplicate_payment_screen",
            "requirement": "duplicate_payment_screen",
            "severity": "high",
            "description": _duplicate_conflict_text(duplicate),
            "quoted_text": str(duplicate.get("source_quote") or ""),
            "affected_fields": ["prior_payment_doc", "clearing_doc"],
        }
    ]


def _merged_extracted_fields(items: list[dict[str, Any]], duplicate: dict[str, Any]) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in items:
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        fields = metadata.get("extracted_fields") if isinstance(metadata.get("extracted_fields"), dict) else {}
        for key, value in fields.items():
            merged.setdefault(key, value)
    if duplicate.get("prior_payment_doc"):
        merged["prior_payment_doc"] = _field(duplicate["prior_payment_doc"], str(duplicate.get("source_quote") or ""), "duplicate_payment_check", "high")
    if duplicate.get("clearing_doc"):
        merged["clearing_doc"] = _field(duplicate["clearing_doc"], str(duplicate.get("source_quote") or ""), "duplicate_payment_check", "high")
    return merged


def _case_summary(items: list[dict[str, Any]], duplicate: dict[str, Any]) -> str:
    invoice = _find_field(items, "invoice_number")
    supplier = _find_field(items, "supplier")
    amount = _find_field(items, "amount_total")
    parts = ["Text-direct evidence reviewed"]
    if invoice:
        parts.append(f"invoice {invoice}")
    if supplier:
        parts.append(f"supplier {supplier}")
    if amount:
        parts.append(f"amount {amount}")
    if duplicate:
        parts.append("duplicate payment risk found")
    return "; ".join(parts)


def _case_profile(items: list[dict[str, Any]], duplicate: dict[str, Any]) -> dict[str, Any]:
    return {
        "invoice_number": _find_field(items, "invoice_number"),
        "supplier": _find_field(items, "supplier"),
        "amount_total": _find_field(items, "amount_total"),
        "po_ref": _find_field(items, "po_ref"),
        "grn_ref": _find_field(items, "grn_ref"),
        "duplicate_payment": duplicate,
        "runtime_recovery": "text_direct_review_after_specialist_timeout",
    }


def _find_field(items: list[dict[str, Any]], field: str) -> Any:
    for item in items:
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        fields = metadata.get("extracted_fields") if isinstance(metadata.get("extracted_fields"), dict) else {}
        if field in fields:
            return fields[field].get("value")
    return ""


def _next_questions(duplicate: dict[str, Any]) -> list[str]:
    if not duplicate:
        return []
    return ["Please reconcile the historical payment and clearing document before treating the invoice as clear."]


def _reply_brief(duplicate: dict[str, Any]) -> str:
    if not duplicate:
        return "Materials were reviewed from text-direct attachments and saved to the case state."
    prior = duplicate.get("prior_payment_doc") or "a prior payment document"
    clearing = duplicate.get("clearing_doc") or "a clearing document"
    return f"Duplicate payment risk found: historical payment {prior} and clearing document {clearing} require reconciliation."


def _reason(error: Exception | None) -> str:
    if not error:
        return "Runtime text-direct recovery produced a schema-valid review."
    return f"Runtime text-direct recovery produced a schema-valid review after {type(error).__name__}: {error}"


def _summary_for_item(evidence_type: str, item: dict[str, Any], text: str) -> str:
    name = str(item.get("name") or evidence_type)
    return f"{evidence_type} evidence from {name}: {_compact_text(text, 220)}"


def _quote_for_requirement(requirement: str, item: dict[str, Any], text: str) -> str:
    if requirement in {"duplicate_payment_check", "duplicate_payment_screen"}:
        duplicate = _duplicate_facts([item])
        if duplicate.get("source_quote"):
            return str(duplicate["source_quote"])
    return _compact_text(text, 260)


def _quoted_text(item: dict[str, Any], text: str) -> list[str]:
    quotes = [_compact_text(text, 360)]
    duplicate = _duplicate_facts([item])
    if duplicate.get("source_quote"):
        quotes.append(str(duplicate["source_quote"]))
    return [quote for quote in dict.fromkeys(quotes) if quote]


def _evidence_card(item: dict[str, Any]) -> dict[str, Any]:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    return {
        "title": f"{item.get('type')} - {metadata.get('name') or item.get('reference') or 'attachment'}",
        "doc_type": item.get("type"),
        "extracted_summary": item.get("summary", ""),
        "supports": [support.get("requirement") for support in item.get("supports") or [] if isinstance(support, dict)],
        "conflicts": list(item.get("conflicts") or [])[:4],
        "source_ref": metadata.get("original_ref", ""),
        "visual_summary": "text-direct source; no preview crop required",
    }


def _compact_field_inventory(value: Any) -> list[dict[str, Any]]:
    rows = value if isinstance(value, list) else []
    result: list[dict[str, Any]] = []
    for row in rows[:12]:
        if not isinstance(row, dict):
            continue
        result.append(
            {
                key: _compact_text(row.get(key), 180)
                for key in ("field", "value", "status", "source_quote", "locator", "confidence", "preview_path")
                if row.get(key) not in (None, "", [], {})
            }
        )
    return result


def _compact_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 14)].rstrip() + "...[truncated]"
