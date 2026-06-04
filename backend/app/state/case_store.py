from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.domain.invoice_requirements import (
    AP_THREE_WAY_REQUIREMENTS,
    INVOICE_FIELD_REQUIREMENTS,
    default_requirement_required,
    requirement_label,
)
from app.domain.risk_rules import derived_conflicts, resolved_conflict_note
from app.state.attachment_manifest import link_manifest_evidence
from app.state.schemas import CasePatch, CaseState, EvidenceItem, Requirement, new_case_state


SAFE_CASE_ID = re.compile(r"^[A-Za-z0-9_.:-]+$")
SAFE_REQUIREMENT_ID = re.compile(r"^[A-Za-z0-9_.:-]+$")
REQUIREMENT_ID_ALIASES = {
    "grn": "goods_receipt_or_service_acceptance",
    "grn_supplier": "goods_receipt_or_service_acceptance",
    "grn_quantity": "goods_receipt_or_service_acceptance",
    "grn_amount": "goods_receipt_or_service_acceptance",
}
OPTIONAL_DYNAMIC_SUPPORT_REQUIREMENTS = {
    "invoice_number",
    "supplier",
    "buyer",
    "invoice_date",
    "amount_total",
    "currency_tax",
    "line_items_product_title",
    "signature_or_authorized_signatory",
    "source_traceability",
    "template_match",
    "po_number",
    "po_amount",
    "po_date",
    "grn_number",
    "received_quantity",
    "inspection_status",
    "vendor_status",
    "vendor_id",
    "bank_last_four",
    "duplicate_invoice_found",
    "prior_payment_found",
    "clearing_document",
    "payment_reference",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FileBoundaryError(ValueError):
    pass


class CaseStore:
    def __init__(self, workspace_root: Path | None = None) -> None:
        self.workspace_root = (workspace_root or get_settings().workspace_root).resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    def validate_case_id(self, case_id: str) -> str:
        value = str(case_id or "").strip() or "case_001"
        if not SAFE_CASE_ID.match(value) or ".." in value or "/" in value or "\\" in value:
            raise FileBoundaryError(f"Unsafe case_id: {case_id}")
        return value

    def case_dir(self, case_id: str) -> Path:
        safe_id = self.validate_case_id(case_id)
        path = (self.workspace_root / safe_id).resolve()
        if self.workspace_root not in path.parents and path != self.workspace_root:
            raise FileBoundaryError("Case path escapes workspace root")
        return path

    def ensure_case_dirs(self, case_id: str) -> Path:
        root = self.case_dir(case_id)
        for name in ("attachments", "evidence", "reports", "traces"):
            (root / name).mkdir(parents=True, exist_ok=True)
        return root

    def resolve_case_path(self, case_id: str, relative_path: str) -> Path:
        root = self.ensure_case_dirs(case_id)
        candidate = (root / relative_path).resolve()
        if root not in candidate.parents and candidate != root:
            raise FileBoundaryError("Path escapes case workspace")
        return candidate

    def load(self, case_id: str) -> CaseState:
        root = self.ensure_case_dirs(case_id)
        path = root / "case_state.json"
        if not path.exists():
            state = new_case_state(self.validate_case_id(case_id))
            self.save(state)
            return state
        data = json.loads(path.read_text(encoding="utf-8"))
        migrated = _migrate_case_state_data(data)
        state = CaseState.model_validate(migrated)
        metadata_changed = _normalize_existing_evidence_metadata(state)
        supports_changed = _backfill_existing_invoice_supports(state)
        cleanup_changed = _drop_legacy_page_edge_warnings(state)
        before_refresh = state.model_dump(mode="json")
        self._refresh_requirements(state)
        refresh_changed = state.model_dump(mode="json") != before_refresh
        buckets_changed = _refresh_material_buckets(state)
        if migrated != data or metadata_changed or supports_changed or cleanup_changed or refresh_changed or buckets_changed:
            self.save(state)
        return state

    def save(self, state: CaseState) -> None:
        root = self.ensure_case_dirs(state.case_id)
        (root / "case_state.json").write_text(
            state.model_dump_json(indent=2),
            encoding="utf-8",
        )

    def next_trace_case_seq(self, case_id: str) -> int:
        path = self.resolve_case_path(case_id, "traces/events.jsonl")
        if not path.exists():
            return 1
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip()) + 1

    def apply_patch(self, case_id: str, patch_data: dict[str, Any] | CasePatch) -> CaseState:
        patch = patch_data if isinstance(patch_data, CasePatch) else CasePatch.model_validate(patch_data)
        state = self.load(case_id)
        updates = patch.case_updates.model_dump(exclude_none=True)
        if updates.get("summary") is not None:
            state.summary = str(updates["summary"] or "")
        if updates.get("conversation_summary") is not None:
            state.conversation_summary = str(updates["conversation_summary"] or "")
        if updates.get("case_profile") is not None:
            state.case_profile = _compact_jsonish_dict(updates.get("case_profile") or {}, max_chars=4000)
        if updates.get("next_action_hint") is not None:
            state.next_action_hint = str(updates.get("next_action_hint") or "")[:240]
        if updates.get("reply_brief") is not None:
            state.reply_brief = str(updates.get("reply_brief") or "")[:1200]
        if updates.get("evidence_cards") is not None:
            state.evidence_cards = _compact_jsonish_records(updates.get("evidence_cards") or [], limit=24, max_chars=1800)
        self._apply_requirement_updates(state, updates)
        if "risk_flags" in updates:
            state.risk_flags = _unique_strings(list(state.risk_flags) + list(updates.get("risk_flags") or []))
        if "next_questions" in updates:
            state.next_questions = _unique_strings(list(updates.get("next_questions") or []))
        if "evidence_items" in updates:
            self._ensure_requirements_for_evidence(state, updates.get("evidence_items") or [])
            for raw in updates.get("evidence_items") or []:
                data = _normalize_evidence_data(raw)
                _normalize_evidence_metadata(data)
                _backfill_supports_from_extracted_fields(state, data)
                if not data.get("id"):
                    data["id"] = self.next_evidence_id(state)
                if not data.get("created_at"):
                    data["created_at"] = utc_now()
                item = EvidenceItem.model_validate(data)
                state.evidence_items.append(item)
        if "add_evidence" in updates:
            self._ensure_requirements_for_evidence(state, updates.get("add_evidence") or [])
            for raw in updates.get("add_evidence") or []:
                data = _normalize_evidence_data(raw)
                _normalize_evidence_metadata(data)
                _backfill_supports_from_extracted_fields(state, data)
                if not data.get("id"):
                    data["id"] = self.next_evidence_id(state)
                if not data.get("created_at"):
                    data["created_at"] = utc_now()
                state.evidence_items.append(EvidenceItem.model_validate(data))
        self._refresh_requirements(state)
        self.save(state)
        self.append_audit(case_id, patch)
        return state

    def _apply_requirement_updates(self, state: CaseState, updates: dict[str, Any]) -> None:
        existing = {item.id: item for item in state.requirements}
        for raw_id in updates.get("remove_requirements") or []:
            requirement_id = _normalize_requirement_id(raw_id)
            if not requirement_id or requirement_id not in existing:
                continue
            if _requirement_has_evidence(state, requirement_id):
                raise ValueError(f"Cannot remove requirement with evidence: {requirement_id}")
            state.requirements = [item for item in state.requirements if item.id != requirement_id]
            existing.pop(requirement_id, None)

        for raw in updates.get("requirements") or []:
            data = raw.model_dump(exclude_none=True) if hasattr(raw, "model_dump") else dict(raw or {})
            requirement_id = _normalize_requirement_id(data.get("id"))
            if not requirement_id:
                continue
            data["id"] = requirement_id
            data["label"] = str(data.get("label") or requirement_label(requirement_id))
            data.setdefault("required", default_requirement_required(requirement_id))
            if requirement_id in existing:
                current = existing[requirement_id]
                for field in ("label", "kind", "required", "guidance"):
                    if field in data:
                        setattr(current, field, data[field])
            else:
                requirement = Requirement.model_validate(data)
                state.requirements.append(requirement)
                existing[requirement_id] = requirement

    def _ensure_requirements_for_evidence(self, state: CaseState, raw_items: list[Any]) -> None:
        support_ids = _support_requirement_ids(raw_items)
        derived_support_ids = False
        if not support_ids:
            support_ids = _support_requirement_ids_from_extracted_fields(raw_items)
            derived_support_ids = bool(support_ids)
        if not support_ids:
            return
        existing = {item.id for item in state.requirements}
        if _looks_like_wrong_ap_default(state, existing, support_ids):
            state.requirements = []
            existing = set()
        if existing:
            unknown = sorted(requirement_id for requirement_id in support_ids if requirement_id not in existing)
            if unknown:
                added = self._add_optional_dynamic_requirements(state, unknown)
                existing.update(added)
                unknown = [requirement_id for requirement_id in unknown if requirement_id not in added]
            if unknown:
                if derived_support_ids:
                    support_ids = [requirement_id for requirement_id in support_ids if requirement_id in existing]
                    if support_ids:
                        return
                    return
                raise ValueError(f"Evidence support references unknown requirements: {unknown}")
            return
        requirement_ids = _initial_requirement_ids_from_evidence(raw_items, support_ids)
        for requirement_id in requirement_ids:
            state.requirements.append(
                Requirement(
                    id=requirement_id,
                    label=requirement_label(requirement_id),
                    kind=_infer_requirement_kind(requirement_id),
                    required=default_requirement_required(requirement_id),
                )
            )

    def next_evidence_id(self, state: CaseState) -> str:
        return f"ev_{len(state.evidence_items) + 1:03d}"

    def _add_optional_dynamic_requirements(self, state: CaseState, requirement_ids: list[str]) -> set[str]:
        added: set[str] = set()
        existing = {item.id for item in state.requirements}
        for raw_id in requirement_ids:
            requirement_id = _normalize_requirement_id(raw_id)
            if requirement_id in existing or requirement_id not in OPTIONAL_DYNAMIC_SUPPORT_REQUIREMENTS:
                continue
            state.requirements.append(
                Requirement(
                    id=requirement_id,
                    label=requirement_label(requirement_id),
                    kind=_infer_requirement_kind(requirement_id),
                    required=False,
                )
            )
            existing.add(requirement_id)
            added.add(requirement_id)
        return added

    def append_audit(self, case_id: str, patch: CasePatch) -> None:
        root = self.ensure_case_dirs(case_id)
        record = {
            "ts": utc_now(),
            "patch_type": patch.patch_type,
            "audit_note": patch.audit_note,
            "case_updates": patch.case_updates.model_dump(exclude_none=True),
        }
        with (root / "traces" / "case_audit.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _refresh_requirements(self, state: CaseState) -> None:
        _apply_supersession_metadata(state)
        active_evidence = _active_evidence_items(state)
        evidence_by_requirement: dict[str, list[EvidenceItem]] = {}
        for item in active_evidence:
            for support in item.supports:
                if support.requirement:
                    evidence_by_requirement.setdefault(support.requirement, []).append(item)
        for req in state.requirements:
            related = _unique_evidence(evidence_by_requirement.get(req.id, []))
            ids = [item.id for item in related]
            legacy_ids = list(req.evidence_ids or [])
            req.evidence_ids = ids
            if not ids:
                req.status = "submitted" if legacy_ids else "missing"
                if legacy_ids:
                    req.evidence_ids = legacy_ids
                continue
            req.status = _requirement_status(related, req.id)
        buckets = _material_buckets(state)
        state.missing_materials = buckets["missing_materials"]
        state.weak_materials = buckets["weak_materials"]
        state.conflict_materials = buckets["conflict_materials"]
        state.satisfied_materials = buckets["satisfied_materials"]
        blockers = state.missing_materials + state.weak_materials + state.conflict_materials
        if blockers:
            state.status = "collecting_materials" if state.evidence_items else "new"
        elif state.evidence_items and state.requirements:
            state.status = "ready_for_report"
        elif state.evidence_items:
            state.status = "collecting_materials"
        link_manifest_evidence(self, state.case_id, state)


def _unique_strings(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if isinstance(value, str):
            text = value.strip()
        else:
            text = json.dumps(value, ensure_ascii=False, default=str).strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _compact_jsonish_dict(value: Any, *, max_chars: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return value
    return {"summary": text[: max_chars - 16] + "...[truncated]"}


def _compact_jsonish_records(value: Any, *, limit: int, max_chars: int) -> list[dict[str, Any]]:
    items = value if isinstance(value, list) else [value]
    records: list[dict[str, Any]] = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        text = json.dumps(item, ensure_ascii=False, default=str)
        if len(text) <= max_chars:
            records.append(item)
        else:
            records.append({"summary": text[: max_chars - 16] + "...[truncated]"})
    return records


def _material_buckets(state: CaseState) -> dict[str, list[str]]:
    buckets = {
        "missing_materials": [],
        "weak_materials": [],
        "conflict_materials": [],
        "satisfied_materials": [],
    }
    for requirement in state.requirements:
        if not requirement.required and requirement.status in {"missing", "weak"}:
            continue
        if requirement.status == "missing":
            buckets["missing_materials"].append(requirement.id)
        elif requirement.status == "conflict":
            buckets["conflict_materials"].append(requirement.id)
        elif requirement.status in {"accepted", "satisfied"}:
            buckets["satisfied_materials"].append(requirement.id)
        else:
            buckets["weak_materials"].append(requirement.id)
    return {key: _unique_strings(value) for key, value in buckets.items()}


def _active_evidence_items(state: CaseState) -> list[EvidenceItem]:
    superseded = _superseded_evidence_ids(state.evidence_items)
    return [item for item in state.evidence_items if item.id not in superseded]


def _superseded_evidence_ids(items: list[EvidenceItem]) -> set[str]:
    result: set[str] = set()
    valid_ids = {item.id for item in items}
    for item in items:
        if not _can_supersede_evidence(item):
            continue
        for old_id in _metadata_supersedes_ids(item.metadata):
            if old_id in valid_ids and old_id != item.id:
                result.add(old_id)
    return result


def _apply_supersession_metadata(state: CaseState) -> bool:
    superseded_by: dict[str, str] = {}
    valid_ids = {item.id for item in state.evidence_items}
    for item in state.evidence_items:
        if not _can_supersede_evidence(item):
            continue
        for old_id in _metadata_supersedes_ids(item.metadata):
            if old_id in valid_ids and old_id != item.id:
                superseded_by[old_id] = item.id
    if not superseded_by:
        return False
    changed = False
    for index, item in enumerate(list(state.evidence_items)):
        new_id = superseded_by.get(item.id)
        if not new_id:
            continue
        data = item.model_dump()
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        if metadata.get("review_stage") == "superseded" and metadata.get("superseded_by_evidence_id") == new_id:
            continue
        metadata["review_stage"] = "superseded"
        metadata["superseded_by_evidence_id"] = new_id
        data["metadata"] = metadata
        state.evidence_items[index] = EvidenceItem.model_validate(data)
        changed = True
    return changed


def _can_supersede_evidence(item: EvidenceItem) -> bool:
    if not _metadata_supersedes_ids(item.metadata):
        return False
    review_result = item.review_result if isinstance(item.review_result, dict) else {}
    if review_result and review_result.get("should_accept") is False:
        return False
    return True


def _metadata_supersedes_ids(metadata: Any) -> list[str]:
    if not isinstance(metadata, dict):
        return []
    values = _string_list(metadata.get("supersedes_evidence_id"))
    values.extend(_string_list(metadata.get("supersedes_evidence_ids")))
    return _unique_strings(values)


def _refresh_material_buckets(state: CaseState) -> bool:
    before = (
        list(state.missing_materials),
        list(getattr(state, "weak_materials", []) or []),
        list(getattr(state, "conflict_materials", []) or []),
        list(getattr(state, "satisfied_materials", []) or []),
    )
    buckets = _material_buckets(state)
    state.missing_materials = buckets["missing_materials"]
    state.weak_materials = buckets["weak_materials"]
    state.conflict_materials = buckets["conflict_materials"]
    state.satisfied_materials = buckets["satisfied_materials"]
    after = (
        list(state.missing_materials),
        list(state.weak_materials),
        list(state.conflict_materials),
        list(state.satisfied_materials),
    )
    return before != after


def _normalize_evidence_data(raw: Any) -> dict[str, Any]:
    data = raw.model_dump(exclude_none=True) if hasattr(raw, "model_dump") else dict(raw or {})
    if not data.get("type") and data.get("evidence_type"):
        data["type"] = data.get("evidence_type")
    if not data.get("summary") and data.get("reference"):
        data["summary"] = str(data.get("reference"))
    data["conflicts"] = _string_list(data.get("conflicts"))
    data["quoted_text"] = _string_list(data.get("quoted_text"))
    data["supports"] = _normalize_support_records(data.get("supports"))
    if _is_cross_case_evidence(data):
        data["supports"] = []
        data["conflicts"] = _unique_strings(
            data["conflicts"] + ["Cross-case or mixed-case evidence is recorded but not linked to active requirements."]
        )
    if _is_prompt_injection_evidence(data):
        _quarantine_prompt_injection_evidence(data)
    else:
        data["conflicts"] = _unique_strings(data["conflicts"] + derived_conflicts(data))
    _backfill_conflict_requirement_support(data)
    if not _is_cross_case_evidence(data) and not _is_prompt_injection_evidence(data):
        _backfill_accepted_core_document_support(data)
    return data


def _is_prompt_injection_evidence(data: dict[str, Any]) -> bool:
    metadata = _metadata_dict(data)
    classification = str(metadata.get("classification") or "").strip().lower()
    if classification in {"prompt_injection", "quarantined"}:
        return True
    extracted = metadata.get("extracted_fields")
    if isinstance(extracted, dict) and "injection_detected" in extracted:
        return True
    text = " ".join(
        _jsonish_text(value)
        for value in (
            data.get("summary"),
            data.get("content"),
            data.get("reviewer_notes"),
            data.get("conflicts"),
            data.get("risk_flags"),
            data.get("review_result"),
        )
        if value
    ).lower()
    markers = (
        "ignore previous",
        "ignore all previous",
        "disregard previous",
        "system prompt",
        "developer message",
        "directly approve",
        "指令污染",
        "越权执行",
        "忽略前文",
        "忽略规则",
        "直接批准",
    )
    return any(marker in text for marker in markers)


def _quarantine_prompt_injection_evidence(data: dict[str, Any]) -> None:
    metadata = _metadata_dict(data)
    original_ref = metadata.get("original_ref") or metadata.get("source_locator") or data.get("source") or ""
    metadata["classification"] = "quarantined"
    metadata["extracted_fields"] = {
        "injection_detected": {
            "value": True,
            "status": "present",
            "source_quote": "[redacted prompt injection]",
            "source_locator": str(original_ref or "attachment text"),
            "confidence": "high",
        },
        "document_confidence": {
            "value": "low",
            "status": "present",
            "source_quote": "quarantined source",
            "source_locator": str(original_ref or "attachment text"),
            "confidence": "high",
        },
    }
    data["type"] = "unknown"
    data["credibility"] = "low"
    data["summary"] = "污染材料中的业务字段已整体隔离"
    data["content"] = "该附件包含可疑指令文本；其中的业务字段不会用于满足任何 requirement。"
    data["supports"] = []
    data["quoted_text"] = ["[redacted prompt injection]"]
    data["reviewer_notes"] = "该来源已隔离；业务字段、金额、供应商、日期等内容不进入证据支持链。"
    data["conflicts"] = _unique_strings(["prompt_injection_quarantine"])
    data["metadata"] = metadata


def _normalize_existing_evidence_metadata(state: CaseState) -> bool:
    changed = False
    for index, item in enumerate(list(state.evidence_items)):
        data = item.model_dump()
        if _normalize_evidence_metadata(data):
            state.evidence_items[index] = EvidenceItem.model_validate(data)
            changed = True
    return changed


def _normalize_evidence_metadata(data: dict[str, Any]) -> bool:
    metadata = _metadata_dict(data)
    if not metadata:
        return False
    existing = metadata.get("field_inventory")
    rows: list[dict[str, Any]] = []
    existing_field_keys: set[str] = set()
    if isinstance(existing, list):
        for row in existing:
            if not isinstance(row, dict) or not _is_curated_field_inventory_row(row):
                continue
            field_key = re.sub(
                r"[^a-z0-9_]+",
                "_",
                str(row.get("field") or row.get("requirement") or "").lower(),
            ).strip("_")
            if field_key and _field_already_covered_in_inventory(field_key, existing_field_keys):
                continue
            rows.append(dict(row))
            if field_key:
                existing_field_keys.add(field_key)
    seen = {str(row.get("crop_path") or "") for row in rows if str(row.get("crop_path") or "")}
    seen_fields = set(existing_field_keys)

    def add(row: Any, *, field: str = "", secondary: bool = False) -> None:
        if not isinstance(row, dict):
            return
        if not _is_curated_field_inventory_row(row, field=field):
            return
        field_key = re.sub(
            r"[^a-z0-9_]+",
            "_",
            str(field or row.get("field") or row.get("requirement") or "").lower(),
        ).strip("_")
        if secondary and field_key and _field_already_covered_in_inventory(field_key, seen_fields):
            return
        crop_path = str(row.get("crop_path") or "").strip()
        if not crop_path or crop_path in seen:
            return
        record = dict(row)
        if field and not record.get("field"):
            record["field"] = field
        if not record.get("source_quote") and record.get("quote"):
            record["source_quote"] = record.get("quote")
        if not record.get("source_quote") and record.get("text"):
            record["source_quote"] = record.get("text")
        if not record.get("value") and record.get("source_quote"):
            record["value"] = record.get("source_quote")
        if not record.get("locator") and record.get("block_or_table_or_region"):
            page = str(record.get("page") or "").strip()
            block = str(record.get("block_or_table_or_region") or "").strip()
            record["locator"] = f"page {page} {block}".strip()
        if not record.get("proof_label"):
            record["proof_label"] = record.get("proves") or record.get("proof") or record.get("claim") or f"{record.get('field') or 'field'} source crop"
        record.setdefault("crop_status", "cropped")
        rows.append(record)
        seen.add(crop_path)
        if field_key:
            seen_fields.add(field_key)

    extracted = metadata.get("extracted_fields")
    if isinstance(extracted, dict):
        for field, value in extracted.items():
            add(value, field=str(field))
    for key in ("evidence_chain", "claim_to_source_refs"):
        values = metadata.get(key)
        if not isinstance(values, list):
            continue
        for row in values:
            add(row, secondary=True)
    if rows and rows != existing:
        metadata["field_inventory"] = rows
        data["metadata"] = metadata
        return True
    return False


def _is_curated_field_inventory_row(row: dict[str, Any], *, field: str = "") -> bool:
    field_name = str(field or row.get("field") or row.get("requirement") or "").strip()
    if not field_name:
        return False
    crop_id = str(row.get("crop_id") or "").strip()
    name = f"{field_name} {crop_id}".lower()
    if "_context" in name or "[truncated]" in name or "[截断]" in name:
        return False
    if re.fullmatch(r"p\d+_b\d+", crop_id.lower()) and not field_name:
        return False
    if "page_number" in name:
        return False
    known = {
        "invoice_number",
        "supplier",
        "buyer",
        "invoice_date",
        "amount_total",
        "currency_tax",
        "currency",
        "tax_amount",
        "line_items_product_title",
        "signature_or_authorized_signatory",
        "purchase_order",
        "goods_receipt_or_service_acceptance",
        "vendor_identity",
        "duplicate_payment_screen",
        "source_traceability",
        "template_match",
        "bank_details",
    }
    normalized = re.sub(r"[^a-z0-9_]+", "_", field_name.lower()).strip("_")
    if normalized in known:
        return True
    proof = str(row.get("proof_label") or row.get("proves") or row.get("proof") or row.get("claim") or "")
    return bool(proof.strip()) and "context" not in proof.lower()


def _field_already_covered_in_inventory(field_key: str, seen_fields: set[str]) -> bool:
    if field_key in seen_fields:
        return True
    aliases = {
        "currency": {"currency_tax"},
        "tax_amount": {"currency_tax"},
        "tax_details": {"currency_tax"},
        "visual_signature_mark": {"signature_or_authorized_signatory"},
    }
    return bool(aliases.get(field_key, set()).intersection(seen_fields))


def _normalize_requirement_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = REQUIREMENT_ID_ALIASES.get(text, text)
    if not SAFE_REQUIREMENT_ID.match(text) or ".." in text or "/" in text or "\\" in text:
        raise ValueError(f"Unsafe requirement id: {value}")
    return text


def _normalize_support_records(value: Any) -> list[Any]:
    items = value if isinstance(value, list) else ([] if value is None else [value])
    normalized: list[Any] = []
    for item in items:
        data = item.model_dump(exclude_none=True) if hasattr(item, "model_dump") else dict(item or {})
        requirement_id = _normalize_requirement_id(data.get("requirement"))
        if not requirement_id:
            continue
        data["requirement"] = requirement_id
        normalized.append(data)
    return normalized


def _backfill_conflict_requirement_support(data: dict[str, Any]) -> None:
    conflicts = list(data.get("conflicts") or [])
    if not conflicts:
        return
    supports = _normalize_support_records(data.get("supports"))
    existing = {str(item.get("requirement") or "") for item in supports}
    changed = False
    for conflict in conflicts:
        requirement_id = _conflict_requirement_id(conflict)
        if not requirement_id or requirement_id in existing:
            continue
        supports.append(
            {
                "requirement": requirement_id,
                "support_level": "partial",
                "quoted_text": _conflict_quote(conflict) or _support_quote(data),
            }
        )
        existing.add(requirement_id)
        changed = True
    if changed:
        data["supports"] = supports


def _conflict_requirement_id(conflict: Any) -> str:
    data = _conflict_data(conflict)
    if isinstance(data, dict):
        requirement_id = _safe_requirement_id(data.get("requirement"))
        if requirement_id:
            return requirement_id
        affected = data.get("affected_fields") or data.get("affected_requirements") or []
        for item in affected if isinstance(affected, list) else [affected]:
            requirement_id = _safe_requirement_id(item)
            if requirement_id:
                return requirement_id
    text = _jsonish_text(conflict).lower()
    if "duplicate_payment_screen" in text or "duplicate_payment_check" in text:
        return "duplicate_payment_screen"
    return ""


def _conflict_quote(conflict: Any) -> str:
    data = _conflict_data(conflict)
    if isinstance(data, dict):
        for key in ("quoted_text", "description", "details", "required_follow_up"):
            text = str(data.get(key) or "").strip()
            if text:
                return text[:600]
    return _jsonish_text(conflict)[:600]


def _conflict_data(conflict: Any) -> Any:
    if isinstance(conflict, dict):
        return conflict
    if isinstance(conflict, str):
        try:
            return json.loads(conflict)
        except json.JSONDecodeError:
            return conflict
    if hasattr(conflict, "model_dump"):
        return conflict.model_dump(exclude_none=True)
    return conflict


def _safe_requirement_id(value: Any) -> str:
    try:
        return _normalize_requirement_id(value)
    except ValueError:
        return ""


def _backfill_accepted_core_document_support(data: dict[str, Any]) -> None:
    if data.get("conflicts"):
        return
    review = data.get("review_result") if isinstance(data.get("review_result"), dict) else {}
    if not bool(review.get("should_accept")):
        return
    requirement_id = _core_requirement_for_evidence(data)
    if not requirement_id:
        return
    supports = _normalize_support_records(data.get("supports"))
    quote = _support_quote(data)
    for support in supports:
        if support.get("requirement") != requirement_id:
            continue
        support["support_level"] = "full"
        if not support.get("quoted_text"):
            support["quoted_text"] = quote
        data["supports"] = supports
        return
    supports.append({"requirement": requirement_id, "support_level": "full", "quoted_text": quote})
    data["supports"] = supports


def _core_requirement_for_evidence(data: dict[str, Any]) -> str:
    review = data.get("review_result") if isinstance(data.get("review_result"), dict) else {}
    evidence_type = str(data.get("type") or review.get("evidence_type") or "").strip()
    return {
        "invoice": "invoice",
        "purchase_order": "purchase_order",
        "goods_receipt": "goods_receipt_or_service_acceptance",
        "service_acceptance": "goods_receipt_or_service_acceptance",
        "vendor_record": "vendor_identity",
        "duplicate_payment_check": "duplicate_payment_screen",
    }.get(evidence_type, "")


def _support_quote(data: dict[str, Any]) -> str:
    review = data.get("review_result") if isinstance(data.get("review_result"), dict) else {}
    for value in (data.get("summary"), review.get("reason"), data.get("content")):
        text = str(value or "").strip()
        if text:
            return text[:600]
    return "Accepted core document evidence."


def _support_requirement_ids(raw_items: list[Any]) -> list[str]:
    result: list[str] = []
    for raw in raw_items:
        data = raw.model_dump(exclude_none=True) if hasattr(raw, "model_dump") else dict(raw or {})
        for support in data.get("supports") or []:
            support_data = support.model_dump(exclude_none=True) if hasattr(support, "model_dump") else dict(support or {})
            requirement_id = _normalize_requirement_id(support_data.get("requirement"))
            if requirement_id:
                result.append(requirement_id)
    return _unique_strings(result)


def _support_requirement_ids_from_extracted_fields(raw_items: list[Any]) -> list[str]:
    result: list[str] = []
    for raw in raw_items:
        data = raw.model_dump(exclude_none=True) if hasattr(raw, "model_dump") else dict(raw or {})
        if not _is_invoice_business_evidence(data):
            continue
        fields = _metadata_extracted_fields(data)
        for requirement_id in INVOICE_FIELD_REQUIREMENTS:
            if _field_support_source(requirement_id, fields, _metadata_dict(data)):
                result.append(requirement_id)
    return _unique_strings(result)


def _backfill_supports_from_extracted_fields(state: CaseState, data: dict[str, Any]) -> None:
    if not _is_invoice_business_evidence(data):
        return
    fields = _metadata_extracted_fields(data)
    if not fields:
        return
    supports: list[Any] = list(data.get("supports") or [])
    existing_requirements = {
        _normalize_requirement_id(
            support.get("requirement") if isinstance(support, dict) else getattr(support, "requirement", "")
        )
        for support in supports
    }
    metadata = _metadata_dict(data)
    for requirement in state.requirements:
        if requirement.id in existing_requirements:
            continue
        source = _field_support_source(requirement.id, fields, metadata)
        if not source:
            continue
        quote, level = source
        supports.append({"requirement": requirement.id, "support_level": level, "quoted_text": quote})
    if supports:
        data["supports"] = supports


def _backfill_existing_invoice_supports(state: CaseState) -> bool:
    changed = False
    for index, item in enumerate(list(state.evidence_items)):
        data = item.model_dump()
        before = data.get("supports") or []
        _backfill_supports_from_extracted_fields(state, data)
        _backfill_conflict_requirement_support(data)
        if not _is_cross_case_evidence(data) and not _is_prompt_injection_evidence(data):
            _backfill_accepted_core_document_support(data)
        after = data.get("supports") or []
        if after != before:
            state.evidence_items[index] = EvidenceItem.model_validate(data)
            changed = True
    return changed


def _drop_legacy_page_edge_warnings(state: CaseState) -> bool:
    changed = False

    def keep(value: Any) -> bool:
        text = str(value or "").lower()
        legacy_false_warnings = (
            "content_near_page_edge",
            "页面边缘内容接近",
            "manifest状态weak",
            "正文存在大字段截断",
            "中间明细区域未完整可见",
            "商品明细截断",
        )
        return not any(marker in text for marker in legacy_false_warnings)

    cleaned_risks = [risk for risk in state.risk_flags if keep(risk)]
    if cleaned_risks != state.risk_flags:
        state.risk_flags = cleaned_risks
        changed = True

    for index, item in enumerate(list(state.evidence_items)):
        data = item.model_dump()
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        item_changed = False
        quality_notes = metadata.get("quality_notes")
        if isinstance(quality_notes, list):
            cleaned_notes = [note for note in quality_notes if keep(note)]
            if cleaned_notes != quality_notes:
                metadata["quality_notes"] = cleaned_notes
                item_changed = True
        visual_check = metadata.get("visual_check")
        if isinstance(visual_check, dict):
            page_integrity = visual_check.get("page_integrity")
            if isinstance(page_integrity, dict):
                warnings = page_integrity.get("warnings")
                if isinstance(warnings, list):
                    cleaned_warnings = [warning for warning in warnings if keep(warning)]
                    if cleaned_warnings != warnings:
                        page_integrity["warnings"] = cleaned_warnings
                        if not cleaned_warnings and page_integrity.get("status") == "warning":
                            page_integrity["status"] = "ok"
                        item_changed = True
        notes = str(data.get("reviewer_notes") or "")
        if not keep(notes):
            cleaned = re.sub(r"[^。；;\n]*content_near_page_edge[^。；;\n]*[。；;]?", "", notes)
            cleaned = re.sub(r"[^。；;\n]*页面边缘内容接近[^。；;\n]*[。；;]?", "", cleaned)
            cleaned = re.sub(r"[^。；;\n]*Manifest状态weak[^。；;\n]*[。；;]?", "", cleaned)
            cleaned = re.sub(r"[^。；;\n]*正文存在大字段截断[^。；;\n]*[。；;]?", "", cleaned)
            cleaned = re.sub(r"[^。；;\n]*中间明细区域未完整可见[^。；;\n]*[。；;]?", "", cleaned)
            cleaned = re.sub(r"[^。；;\n]*商品明细截断[^。；;\n]*[。；;]?", "", cleaned)
            data["reviewer_notes"] = cleaned.strip()
            item_changed = True
        if item_changed:
            data["metadata"] = metadata
            state.evidence_items[index] = EvidenceItem.model_validate(data)
            changed = True
    return changed


def _is_invoice_business_evidence(data: dict[str, Any]) -> bool:
    metadata = _metadata_dict(data)
    classification = str(metadata.get("classification") or "").strip().lower()
    blocked = {
        "prompt_injection",
        "quarantined",
        "irrelevant",
        "wrong_workflow",
        "process_only",
        "policy_guidance",
        "cross_case_sample",
        "mixed_case_document",
        "out_of_scope_reference",
    }
    if classification in blocked or _is_cross_case_evidence(data):
        return False
    review_result = data.get("review_result") if isinstance(data.get("review_result"), dict) else {}
    evidence_type = str(data.get("type") or data.get("evidence_type") or review_result.get("evidence_type") or "")
    return evidence_type.strip() == "invoice"


def _metadata_dict(data: dict[str, Any]) -> dict[str, Any]:
    metadata = data.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _metadata_extracted_fields(data: dict[str, Any]) -> dict[str, Any]:
    metadata = _metadata_dict(data)
    fields = metadata.get("extracted_fields")
    result = dict(fields) if isinstance(fields, dict) else {}
    inventory = metadata.get("field_inventory")
    if isinstance(inventory, list):
        for row in inventory:
            if not isinstance(row, dict):
                continue
            field_id = str(row.get("field") or "").strip()
            if field_id and field_id not in result:
                result[field_id] = row
    return result


def _field_support_source(requirement_id: str, fields: dict[str, Any], metadata: dict[str, Any]) -> tuple[str, str] | None:
    aliases = {
        "invoice_number": ("invoice_number", "document_id"),
        "supplier": ("supplier",),
        "buyer": ("buyer",),
        "invoice_date": ("invoice_date", "date"),
        "amount_total": ("amount_total", "amount"),
        "line_items_product_title": ("line_items_product_title", "product_title", "title"),
        "signature_or_authorized_signatory": (
            "visual_signature_mark",
            "signature_or_authorized_signatory",
            "signature_block",
            "signatory_label",
        ),
    }
    if requirement_id == "invoice":
        return _invoice_document_support(fields, metadata)
    if requirement_id == "currency_tax":
        return _currency_tax_support(fields)
    if requirement_id == "source_traceability":
        original_ref = str(metadata.get("original_ref") or "").strip()
        if original_ref:
            return f"original_ref: {original_ref}", "full"
        preview_paths = metadata.get("preview_paths")
        if isinstance(preview_paths, list) and preview_paths:
            return f"preview_path: {preview_paths[0]}", "partial"
        return None
    if requirement_id == "template_match":
        comparison = metadata.get("profile_comparison")
        if isinstance(comparison, dict):
            profile = str(comparison.get("matched_profile") or comparison.get("profile_id") or "").strip()
            if profile:
                return f"matched_profile: {profile}", "partial"
        return None
    for alias in aliases.get(requirement_id, (requirement_id,)):
        support = _single_field_support(fields.get(alias))
        if support:
            return support
    return None


def _invoice_document_support(fields: dict[str, Any], metadata: dict[str, Any]) -> tuple[str, str] | None:
    parts: list[str] = []
    full_parts = 0
    for key in ("invoice_number", "supplier", "buyer", "invoice_date", "amount_total"):
        support = _single_field_support(fields.get(key))
        if not support:
            continue
        quote, level = support
        parts.append(f"{key}: {quote}")
        if level == "full":
            full_parts += 1
    if len(parts) < 3:
        return None
    has_source = bool(str(metadata.get("original_ref") or "").strip())
    level = "full" if has_source and len(parts) >= 5 and full_parts >= 4 else "partial"
    return "; ".join(parts)[:500], level


def _currency_tax_support(fields: dict[str, Any]) -> tuple[str, str] | None:
    parts: list[str] = []
    has_currency = False
    has_tax = False
    full_parts = 0
    for key in ("currency_tax", "currency", "tax_amount", "tax_details"):
        support = _single_field_support(fields.get(key))
        if not support:
            continue
        quote, level = support
        parts.append(quote)
        full_parts += 1 if level == "full" else 0
        if key in {"currency_tax", "currency"}:
            has_currency = True
        if key in {"currency_tax", "tax_amount", "tax_details"}:
            has_tax = True
    if not parts:
        return None
    level = "full" if has_currency and has_tax and full_parts == len(parts) else "partial"
    return "; ".join(_unique_strings(parts))[:500], level


def _single_field_support(value: Any) -> tuple[str, str] | None:
    if isinstance(value, dict):
        status = str(value.get("status") or "present").strip().lower()
        if status not in {"present", "accepted", "satisfied"}:
            return None
        quote = str(value.get("source_quote") or value.get("value") or "").strip()
        if not quote:
            return None
        confidence = str(value.get("confidence") or "").strip().lower()
        has_locator = bool(str(value.get("locator") or value.get("source_locator") or "").strip())
        has_crop = bool(str(value.get("crop_path") or "").strip())
        if confidence == "low":
            level = "partial"
        else:
            level = "full" if confidence == "high" or (has_locator and has_crop) else "partial"
        return quote[:500], level
    text = str(value or "").strip()
    if text:
        return text[:500], "partial"
    return None


def _initial_requirement_ids_from_evidence(raw_items: list[Any], support_ids: list[str]) -> list[str]:
    has_invoice_evidence = False
    for raw in raw_items:
        data = raw.model_dump(exclude_none=True) if hasattr(raw, "model_dump") else dict(raw or {})
        evidence_type = str(data.get("type") or data.get("evidence_type") or "").strip()
        if evidence_type == "invoice":
            has_invoice_evidence = True
            break
    if has_invoice_evidence or any(item in INVOICE_FIELD_REQUIREMENTS for item in support_ids):
        return _unique_strings(list(INVOICE_FIELD_REQUIREMENTS) + list(support_ids))
    return support_ids


def _looks_like_wrong_ap_default(state: CaseState, existing: set[str], support_ids: list[str]) -> bool:
    if state.evidence_items:
        return False
    if not existing or not existing.issubset(set(AP_THREE_WAY_REQUIREMENTS)):
        return False
    has_invoice_field_support = any(item in INVOICE_FIELD_REQUIREMENTS for item in support_ids)
    has_ap_support = any(item in AP_THREE_WAY_REQUIREMENTS for item in support_ids)
    return has_invoice_field_support and not has_ap_support


def _requirement_has_evidence(state: CaseState, requirement_id: str) -> bool:
    for item in state.evidence_items:
        for support in item.supports:
            if support.requirement == requirement_id:
                return True
    return any(requirement_id in item.evidence_ids for item in state.requirements if item.id == requirement_id)


def _infer_requirement_kind(requirement_id: str) -> str:
    if requirement_id in {
        "invoice",
        "purchase_order",
        "goods_receipt",
        "vendor_record",
        "goods_receipt_or_service_acceptance",
        "vendor_identity",
    }:
        return "document"
    if requirement_id in {"duplicate_payment_check", "duplicate_payment_screen", "template_match"}:
        return "risk_check"
    if requirement_id in {"source_traceability", "signature_or_authorized_signatory"}:
        return "visual"
    return "field"


CONFLICT_TERMS = (
    "冲突",
    "不一致",
    "不符",
    "差异",
    "mismatch",
    "conflict",
    "discrepancy",
    "duplicate found",
    "duplicate invoice found: yes",
    "potential duplicate payment",
    "prior payment found: yes",
    "clearing document found: yes",
    "存在历史付款",
    "疑似重复付款",
    "重复付款检查命中",
    "潜在重复付款",
    "historical payment",
)

NO_CONFLICT_TERMS = (
    "未发现冲突",
    "无冲突",
    "未发现差异",
    "无差异",
    "no conflict",
    "no discrepancy",
    "no duplicate invoice found",
    "no duplicate found",
    "duplicate invoice found: no",
    "no prior payment found",
    "no clearing document exists",
    "prior payment found: no",
    "clearing document found: no",
    "未发现历史付款",
    "未发现重复付款",
    "未发现重复风险",
    "均未发现重复",
    "不存在历史付款",
    "无未解决的重复付款冲突",
    "无未解决的冲突",
    "无数量冲突",
    "无金额冲突",
    "无银行冲突",
    "无供应商冲突",
)


def _derived_conflicts(data: dict[str, Any]) -> list[str]:
    if data.get("conflicts"):
        return []
    text = _evidence_conflict_text(data)
    if not text:
        return []
    lowered = text.lower()
    conflict_text = lowered
    for term in NO_CONFLICT_TERMS:
        conflict_text = conflict_text.replace(term, " ")
    if not any(term in conflict_text for term in CONFLICT_TERMS):
        return []
    summary = _first_non_empty_text(data.get("reviewer_notes"), data.get("summary"), data.get("content"))
    if len(summary) > 220:
        summary = summary[:217].rstrip() + "..."
    return [f"Derived conflict signal from reviewer output: {summary}"]


def _evidence_conflict_text(data: dict[str, Any]) -> str:
    pieces = [
        data.get("summary"),
        data.get("content"),
        data.get("reviewer_notes"),
        data.get("supports"),
        data.get("quoted_text"),
        data.get("metadata"),
        data.get("review_result"),
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


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    result: list[str] = []
    for item in items:
        if isinstance(item, str):
            text = item.strip()
        else:
            text = json.dumps(item, ensure_ascii=False, default=str)
        if text:
            result.append(text)
    return result


def _is_cross_case_evidence(data: dict[str, Any]) -> bool:
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    classification = str(metadata.get("classification") or "").strip().lower()
    if classification in {"cross_case_sample", "mixed_case_document", "out_of_scope_reference"}:
        return True
    review_result = data.get("review_result") if isinstance(data.get("review_result"), dict) else {}
    text = " ".join(
        _jsonish_text(value)
        for value in (
            data.get("summary"),
            data.get("content"),
            data.get("reviewer_notes"),
            data.get("conflicts"),
            review_result.get("reason"),
        )
        if value
    ).lower()
    markers = (
        "cross_case",
        "mixed_case",
        "cross-case",
        "mixed-case",
        "out-of-scope",
        "另案",
        "跨案",
        "误传文档",
        "不属于当前",
        "不同case",
        "不同 case",
        "另一个case",
        "另一个 case",
        "独立case",
        "独立 case",
        "新case",
        "新 case",
        "separate case",
        "not the same case",
    )
    return any(marker in text for marker in markers)


def _migrate_case_state_data(data: Any) -> Any:
    if not isinstance(data, dict):
        return data
    migrated = dict(data)
    requirements = migrated.get("requirements")
    if isinstance(requirements, list):
        migrated["requirements"] = [_migrate_requirement_data(item) for item in requirements]
    return migrated


def _migrate_requirement_data(data: Any) -> Any:
    if not isinstance(data, dict):
        return data
    migrated = dict(data)
    status = str(migrated.get("status") or "").strip()
    legacy_status_map = {
        "partial": "submitted",
        "complete": "satisfied",
        "completed": "satisfied",
    }
    if status in legacy_status_map:
        migrated["status"] = legacy_status_map[status]
    return migrated


def _unique_evidence(items: list[EvidenceItem]) -> list[EvidenceItem]:
    seen: set[str] = set()
    result: list[EvidenceItem] = []
    for item in items:
        if item.id in seen:
            continue
        seen.add(item.id)
        result.append(item)
    return result


def _requirement_status(items: list[EvidenceItem], requirement_id: str) -> str:
    reviewed = [item for item in items if item.review_result]
    if reviewed and all(not bool(item.review_result.get("should_accept", True)) for item in reviewed):
        return "rejected"
    if not reviewed:
        return "submitted"
    accepted = [item for item in reviewed if bool(item.review_result.get("should_accept", False))]
    if not accepted:
        return "rejected"
    if any(_item_has_requirement_conflict(item, requirement_id) for item in accepted):
        return "conflict"
    full_support_items = [
        item
        for item in accepted
        for support in item.supports
        if support.requirement == requirement_id and support.support_level == "full"
    ]
    if full_support_items:
        if any(item.credibility != "low" for item in full_support_items):
            return "satisfied"
        return "weak"
    if any(_item_has_requirement_conflict(item, requirement_id) for item in items):
        return "conflict"
    full_support = any(
        support.requirement == requirement_id and support.support_level == "full"
        for item in accepted
        for support in item.supports
    )
    weak = any(item.credibility == "low" for item in accepted)
    partial = any(
        support.requirement == requirement_id and support.support_level in {"none", "partial"}
        for item in accepted
        for support in item.supports
    )
    if full_support and not weak and not partial:
        return "satisfied"
    if weak or partial:
        return "weak"
    return "accepted"


def _item_has_requirement_conflict(item: EvidenceItem, requirement_id: str) -> bool:
    conflicts = list(getattr(item, "conflicts", []) or [])
    if not conflicts:
        return False
    return any(
        _conflict_mentions_requirement(conflict, requirement_id)
        for conflict in conflicts
        if not resolved_conflict_note(conflict)
    )


def _conflict_mentions_requirement(conflict: Any, requirement_id: str) -> bool:
    text = _jsonish_text(conflict).lower()
    if not text:
        return False
    aliases = {
        "invoice_number": ("invoice_number", "invoice no", "invoice number", "invoice #", "发票号", "发票编号"),
        "supplier": ("supplier", "vendor", "sold by", "供应商", "销售方"),
        "buyer": ("buyer", "customer", "billing address", "购买方", "客户", "买方"),
        "invoice_date": ("invoice_date", "invoice date", "date", "发票日期", "日期"),
        "amount_total": ("amount_total", "amount", "total", "grand total", "金额", "总额", "合计"),
        "currency_tax": ("currency_tax", "currency", "tax", "vat", "gst", "cst", "币种", "税", "税额"),
        "line_items_product_title": ("line_items_product_title", "line item", "product", "title", "qty", "商品", "明细", "行项目"),
        "signature_or_authorized_signatory": (
            "signature_or_authorized_signatory",
            "signature",
            "signatory",
            "authorized signatory",
            "签名",
            "签章",
            "授权签署",
        ),
        "source_traceability": (
            "source_traceability",
            "traceability",
            "source path missing",
            "original_ref missing",
            "original ref missing",
            "来源可追溯",
            "来源缺失",
            "原件缺失",
            "不可追溯",
        ),
        "template_match": ("template_match", "template", "profile", "模板", "版式"),
        "invoice": ("invoice", "发票"),
        "purchase_order": ("purchase_order", "po", "purchase order", "采购订单"),
        "goods_receipt": ("goods_receipt", "grn", "goods receipt", "收货"),
        "goods_receipt_or_service_acceptance": ("goods_receipt_or_service_acceptance", "grn", "service acceptance", "收货", "服务验收"),
        "vendor_identity": ("vendor_identity", "vendor", "supplier", "供应商"),
        "duplicate_payment_screen": ("duplicate_payment_screen", "duplicate", "prior payment", "重复付款", "历史付款"),
        "duplicate_payment_check": ("duplicate_payment_check", "duplicate", "prior payment", "重复付款", "历史付款"),
    }
    terms = (requirement_id, *aliases.get(requirement_id, ()))
    return any(str(term).lower() in text for term in terms if str(term).strip())
