from __future__ import annotations

import json
from typing import Any


class PatchNormalizer:
    def preserve_reviewer_quote_fields(self, patch: dict[str, Any], reviewer_result: dict[str, Any]) -> dict[str, Any]:
        return preserve_reviewer_quote_fields(patch, reviewer_result)

    def compact_for_write(self, patch: dict[str, Any]) -> dict[str, Any]:
        return compact_case_patch_for_write(patch)


def preserve_reviewer_quote_fields(patch: dict[str, Any], reviewer_result: dict[str, Any]) -> dict[str, Any]:
    """Keep reviewer audit details verbatim while patch_writer compresses narrative fields."""

    if not isinstance(patch, dict) or not isinstance(reviewer_result, dict):
        return patch
    updates = patch.get("case_updates")
    reviewer_updates = reviewer_result.get("suggested_patch")
    if not isinstance(updates, dict) or not isinstance(reviewer_updates, dict):
        return patch
    if not updates.get("evidence_cards"):
        cards = reviewer_result.get("evidence_cards") or reviewer_updates.get("evidence_cards")
        if cards:
            updates["evidence_cards"] = cards
        else:
            fallback_cards = _evidence_cards_from_reviewer_updates(reviewer_updates)
            if fallback_cards:
                updates["evidence_cards"] = fallback_cards
    for field in ("add_evidence", "evidence_items"):
        target_items = updates.get(field)
        source_items = reviewer_updates.get(field)
        if not isinstance(target_items, list) or not isinstance(source_items, list):
            continue
        for target, source in zip(target_items, source_items):
            if not isinstance(target, dict) or not isinstance(source, dict):
                continue
            if "quoted_text" in source:
                target["quoted_text"] = source.get("quoted_text") or []
            if source.get("local_source_handle") and not target.get("local_source_handle"):
                target["local_source_handle"] = source["local_source_handle"]
            for target_support, source_support in zip(target.get("supports") or [], source.get("supports") or []):
                if isinstance(target_support, dict) and isinstance(source_support, dict) and "quoted_text" in source_support:
                    target_support["quoted_text"] = source_support.get("quoted_text") or ""
            source_metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
            target_metadata = target.setdefault("metadata", {})
            if not isinstance(target_metadata, dict):
                continue
            for metadata_key in (
                "attachment_id",
                "original_ref",
                "source_filename",
                "visual_check",
                "field_review_matrix",
                "page_review",
                "evidence_chain",
                "block_crops",
                "profile_comparison",
                "self_check",
            ):
                if metadata_key in source_metadata and metadata_key not in target_metadata:
                    target_metadata[metadata_key] = source_metadata.get(metadata_key)
            source_fields = source_metadata.get("extracted_fields") if isinstance(source_metadata, dict) else None
            if not isinstance(source_fields, dict):
                continue
            target_fields = target_metadata.setdefault("extracted_fields", {})
            if not isinstance(target_fields, dict):
                continue
            for key, source_field in source_fields.items():
                if isinstance(source_field, dict) and "source_quote" in source_field:
                    target_fields.setdefault(key, {})["source_quote"] = source_field.get("source_quote") or ""
    return patch


def compact_case_patch_for_write(patch: dict[str, Any]) -> dict[str, Any]:
    """Keep source refs and proof crops, but avoid storing every OCR block in the CasePatch."""

    if not isinstance(patch, dict):
        return patch
    compact = json.loads(json.dumps(patch, ensure_ascii=False, default=str))
    updates = compact.get("case_updates")
    if not isinstance(updates, dict):
        return compact
    if isinstance(updates.get("evidence_cards"), list):
        updates["evidence_cards"] = [_compact_evidence_card(card) for card in updates["evidence_cards"][:8] if isinstance(card, dict)]
    for field in ("add_evidence", "evidence_items"):
        items = updates.get(field)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            metadata = item.get("metadata")
            compiler_source = isinstance(metadata, dict) and bool(metadata.get("compiler_source_sha256"))
            limits = [("summary", 260), ("reviewer_notes", 420)]
            if not compiler_source:
                limits.append(("content", 420))
            for field, limit in limits:
                if field in item:
                    item[field] = _short_patch_text(item.get(field) or "", limit)
            if isinstance(metadata, dict):
                _compact_evidence_metadata(metadata)
    return compact


def _compact_evidence_metadata(metadata: dict[str, Any]) -> None:
    for key in ("claim_to_source_refs", "semantic_judgments", "requirement_verdicts", "proof_proposals"):
        metadata.pop(key, None)
    field_inventory = metadata.get("field_inventory")
    if isinstance(field_inventory, list):
        compact_fields = [_compact_field_row(row) for row in field_inventory if isinstance(row, dict)]
        metadata["field_inventory"] = _prioritize_field_rows(compact_fields)[:12]
    field_crop_paths = {
        str(row.get("crop_path") or "")
        for row in metadata.get("field_inventory", [])
        if isinstance(row, dict) and row.get("crop_path")
    }
    block_crops = metadata.get("block_crops")
    if isinstance(block_crops, list):
        selected = []
        for row in block_crops:
            if not isinstance(row, dict):
                continue
            crop_path = str(row.get("crop_path") or "")
            if crop_path in field_crop_paths or len(selected) < 8:
                selected.append(_compact_crop_row(row))
            if len(selected) >= 16:
                break
        metadata["block_crops"] = selected
    for key, limit in (("evidence_chain", 14), ("field_review_matrix", 12)):
        rows = metadata.get(key)
        if isinstance(rows, list):
            metadata[key] = [_compact_claim_row(row) for row in rows[:limit] if isinstance(row, dict)]
    page_review = metadata.get("page_review")
    if isinstance(page_review, list):
        metadata["page_review"] = [_compact_page_row(row) for row in page_review[:4] if isinstance(row, dict)]
    visual_check = metadata.get("visual_check")
    if isinstance(visual_check, dict):
        metadata["visual_check"] = _compact_visual_check_for_patch(visual_check)
    if isinstance(metadata.get("quality_notes"), list):
        metadata["quality_notes"] = [_short_patch_text(item, 140) for item in metadata["quality_notes"][:10]]
    if isinstance(metadata.get("line_items"), list):
        metadata["line_items"] = [_compact_line_item_row(row) for row in metadata["line_items"][:200] if isinstance(row, dict)]
        metadata["line_item_count"] = int(metadata.get("line_item_count") or len(metadata["line_items"]))
        pages = metadata.get("line_item_pages")
        if isinstance(pages, list):
            metadata["line_item_pages"] = pages[:12]
    if isinstance(metadata.get("profile_comparison"), dict):
        metadata["profile_comparison"] = _compact_mapping(metadata["profile_comparison"], 10, 180)
    if isinstance(metadata.get("self_check"), dict):
        metadata["self_check"] = _compact_mapping(metadata["self_check"], 10, 180)


def _compact_field_row(row: dict[str, Any]) -> dict[str, Any]:
    keep = {
        "field",
        "value",
        "status",
        "source_quote",
        "locator",
        "confidence",
        "crop_path",
        "block_id",
        "preview_path",
        "proof_label",
        "crop_status",
    }
    return {
        key: (_short_patch_text(value, 360) if key == "source_quote" else _short_patch_text(value, 220))
        for key, value in row.items()
        if key in keep and value not in (None, "", [], {})
    }


def _prioritize_field_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    priority = {
        "invoice_number": 0,
        "supplier": 1,
        "buyer": 2,
        "invoice_date": 3,
        "amount_total": 4,
        "currency_tax": 5,
        "line_items_product_title": 6,
        "signature_or_authorized_signatory": 7,
        "bank_details": 8,
        "source_traceability": 9,
    }
    return sorted(rows, key=lambda row: priority.get(str(row.get("field") or ""), 99))


def _compact_line_item_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: _short_patch_text(value, 160)
        for key, value in row.items()
        if key in {"position", "text", "quantity", "unit_price", "total_amount", "page", "table_id"}
        and value not in (None, "", [], {})
    }


def _compact_crop_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: _short_patch_text(value, 260)
        for key, value in row.items()
        if key in {"crop_id", "kind", "page", "bbox", "text", "locator", "crop_path", "preview_path", "confidence", "proves"}
        and value not in (None, "", [], {})
    }


def _compact_claim_row(row: dict[str, Any]) -> dict[str, Any]:
    keep = {
        "id",
        "claim",
        "subject",
        "predicate",
        "entity_key",
        "value_type",
        "typed_value",
        "unit",
        "currency",
        "basis",
        "tax_basis",
        "coverage",
        "attributes",
        "attribute_sources",
        "source_quote",
        "source_locator",
        "field",
        "value",
        "support_level",
        "locator",
        "crop_path",
        "preview_path",
        "proof_label",
        "limitation",
        "source_doc_id",
        "dossier_ref",
        "page",
        "block_or_table_or_region",
        "quote",
        "confidence",
    }
    return {
        key: (_short_patch_text(value, 360) if key == "quote" else _short_patch_text(value, 220))
        for key, value in row.items()
        if key in keep and value not in (None, "", [], {})
    }


def _compact_page_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: _short_patch_text(value, 260)
        for key, value in row.items()
        if key in {"page", "preview_path", "text_preview", "block_count", "table_count", "quality_notes"}
        and value not in (None, "", [], {})
    }


def _compact_visual_check_for_patch(value: dict[str, Any]) -> dict[str, Any]:
    result = {
        "looks_like_invoice": value.get("looks_like_invoice"),
        "visible_sections": value.get("visible_sections"),
        "same_source_check": value.get("same_source_check"),
        "ocr_quality": value.get("ocr_quality"),
        "page_integrity": value.get("page_integrity"),
        "layout_check": value.get("layout_check"),
        "limitations": list(value.get("limitations") or [])[:8],
    }
    field_visibility = value.get("field_visibility")
    if isinstance(field_visibility, list):
        result["field_visibility"] = [_compact_field_row(row) for row in field_visibility[:12] if isinstance(row, dict)]
    return {key: val for key, val in result.items() if val not in (None, "", [], {})}


def _compact_evidence_card(card: dict[str, Any]) -> dict[str, Any]:
    keep = {
        "evidence_id",
        "title",
        "preview_path",
        "doc_type",
        "visual_summary",
        "extracted_summary",
        "requirement_summary",
        "issues",
        "next_action",
    }
    return {
        key: _short_patch_text(value, 260)
        for key, value in card.items()
        if key in keep and value not in (None, "", [], {})
    }


def _compact_mapping(value: dict[str, Any], max_items: int, max_chars: int) -> dict[str, Any]:
    result = {}
    for index, (key, item) in enumerate(value.items()):
        if index >= max_items:
            break
        result[str(key)] = _short_patch_text(item, max_chars)
    return result


def _short_patch_text(value: Any, max_chars: int) -> Any:
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_short_patch_text(item, max_chars) for item in value[:12]]
    if isinstance(value, dict):
        return _compact_mapping(value, 12, max_chars)
    text = str(value)
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 14)].rstrip() + "...[truncated]"


def _evidence_cards_from_reviewer_updates(reviewer_updates: dict[str, Any]) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    items = reviewer_updates.get("add_evidence") or reviewer_updates.get("evidence_items") or []
    if not isinstance(items, list):
        return cards
    for item in items[:8]:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        extracted = metadata.get("extracted_fields") if isinstance(metadata, dict) else {}
        if not isinstance(extracted, dict):
            extracted = {}
        supports = item.get("supports") if isinstance(item.get("supports"), list) else []
        preview_paths = metadata.get("preview_paths") if isinstance(metadata.get("preview_paths"), list) else []
        invoice_no = _field_value(extracted, "invoice_number")
        supplier = _field_value(extracted, "supplier")
        amount = _field_value(extracted, "amount_total")
        title_bits = [str(item.get("type") or item.get("evidence_type") or "evidence")]
        if invoice_no:
            title_bits.append(str(invoice_no))
        card = {
            "title": " ".join(title_bits)[:160],
            "doc_type": str(item.get("type") or item.get("evidence_type") or "unknown")[:80],
            "preview_path": str(preview_paths[0]) if preview_paths else "",
            "visual_summary": str(metadata.get("visual_summary") or _visual_summary_from_check(metadata.get("visual_check")) or metadata.get("source_traceability") or "")[:240],
            "extracted_summary": _compact_card_summary(invoice_no=invoice_no, supplier=supplier, amount=amount),
            "requirement_summary": [
                f"{support.get('requirement')}: {support.get('support_level')}"
                for support in supports
                if isinstance(support, dict) and support.get("requirement")
            ][:12],
            "issues": list(item.get("risk_flags") or [])[:6] if isinstance(item.get("risk_flags"), list) else [],
            "next_action": "",
        }
        notes = str(item.get("reviewer_notes") or "")
        if notes and not card["issues"]:
            card["issues"] = [notes[:240]]
        cards.append(card)
    return cards


def _visual_summary_from_check(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    ocr = value.get("ocr_quality") if isinstance(value.get("ocr_quality"), dict) else {}
    page = value.get("page_integrity") if isinstance(value.get("page_integrity"), dict) else {}
    same_source = value.get("same_source_check") if isinstance(value.get("same_source_check"), dict) else {}
    bits = []
    if value.get("looks_like_invoice"):
        bits.append(f"looks_like_invoice={value.get('looks_like_invoice')}")
    if ocr.get("status"):
        bits.append(f"OCR={ocr.get('status')}")
    if page.get("status"):
        bits.append(f"page={page.get('status')}")
    if same_source.get("status"):
        bits.append(f"same_source={same_source.get('status')}")
    return "; ".join(bits)


def _field_value(fields: dict[str, Any], key: str) -> Any:
    value = fields.get(key)
    if isinstance(value, dict):
        return value.get("value")
    return value


def _compact_card_summary(*, invoice_no: Any, supplier: Any, amount: Any) -> str:
    parts = []
    if invoice_no:
        parts.append(f"invoice {invoice_no}")
    if supplier:
        parts.append(f"supplier {supplier}")
    if amount:
        parts.append(f"amount {amount}")
    return "; ".join(str(part) for part in parts)[:240]
