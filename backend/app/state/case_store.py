from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.domain.invoice_requirements import (
    AP_LITE_REQUIREMENTS,
    AP_THREE_WAY_REQUIREMENTS,
    COMPILER_DERIVED_REQUIREMENTS,
    DYNAMIC_SUPPORT_REQUIREMENTS,
    INVOICE_FIELD_REQUIREMENTS,
    INVOICE_REQUIRED_FIELD_REQUIREMENTS,
    REVIEWER_DERIVED_REQUIREMENTS,
    default_requirement_required,
    is_known_requirement,
    requirement_kind,
    requirement_label,
    requirement_premises,
    requirement_unconfigured_policy_values,
)
from app.domain.invoice_proof_compiler import compile_evidence_proof
from app.domain.risk_rules import resolved_conflict_note
from app.state.attachment_manifest import link_manifest_evidence, trusted_sources_for_evidence
from app.state.persistence import PERSISTENCE_LOCK, append_text, atomic_write_text
from app.state.schemas import CasePatch, CaseState, ConflictRecord, EvidenceItem, Requirement, new_case_state


SAFE_CASE_ID = re.compile(r"^[A-Za-z0-9_.:-]+$")
SAFE_REQUIREMENT_ID = re.compile(r"^[A-Za-z0-9_.:-]+$")
REQUIREMENT_ID_ALIASES = {
    "grn": "goods_receipt_or_service_acceptance",
    "grn_supplier": "goods_receipt_or_service_acceptance",
    "grn_quantity": "goods_receipt_or_service_acceptance",
    "grn_amount": "goods_receipt_or_service_acceptance",
}
SUPPORT_REQUIREMENT_ID_ALIASES = {
    **REQUIREMENT_ID_ALIASES,
    "goods_receipt": "goods_receipt_or_service_acceptance",
    "service_acceptance": "goods_receipt_or_service_acceptance",
    "vendor_record": "vendor_identity",
    "supplier_record": "vendor_identity",
    "vendor_master": "vendor_identity",
    "duplicate_payment_check": "duplicate_payment_screen",
    "duplicate_check": "duplicate_payment_screen",
}
OPTIONAL_DYNAMIC_SUPPORT_REQUIREMENTS = DYNAMIC_SUPPORT_REQUIREMENTS


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

    def validate_attachment_path(self, case_id: str, path: str) -> Path:
        root = self.ensure_case_dirs(case_id).resolve()
        raw = Path(path).expanduser()
        candidate = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
        if candidate != root and root not in candidate.parents:
            raise FileBoundaryError("Attachment path escapes case workspace")
        return candidate

    def load(self, case_id: str) -> CaseState:
        with PERSISTENCE_LOCK:
            return self._load(case_id)

    def _load(self, case_id: str) -> CaseState:
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
        atomic_write_text(root / "case_state.json", state.model_dump_json(indent=2))

    def next_trace_case_seq(self, case_id: str) -> int:
        with PERSISTENCE_LOCK:
            path = self.resolve_case_path(case_id, "traces/events.jsonl")
            if not path.exists():
                return 1
            return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip()) + 1

    def apply_patch(self, case_id: str, patch_data: dict[str, Any] | CasePatch) -> CaseState:
        with PERSISTENCE_LOCK:
            return self._apply_patch(case_id, patch_data)

    def _apply_patch(self, case_id: str, patch_data: dict[str, Any] | CasePatch) -> CaseState:
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
        _ensure_requirement_premises(state)
        if "risk_flags" in updates:
            state.risk_flags = _unique_strings(list(state.risk_flags) + list(updates.get("risk_flags") or []))
        if "next_questions" in updates:
            state.next_questions = _unique_strings(list(updates.get("next_questions") or []))
        if "evidence_items" in updates:
            self._append_evidence_updates(state, updates.get("evidence_items") or [])
        if "add_evidence" in updates:
            self._append_evidence_updates(state, updates.get("add_evidence") or [])
        self._refresh_requirements(state)
        self.save(state)
        self.append_audit(case_id, patch)
        return state

    def _apply_requirement_updates(self, state: CaseState, updates: dict[str, Any]) -> None:
        existing = {item.id: item for item in state.requirements}
        for raw_id in updates.get("remove_requirements") or []:
            requirement_id = _normalize_requirement_id(raw_id)
            if requirement_id in COMPILER_DERIVED_REQUIREMENTS:
                continue
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
            if not is_known_requirement(requirement_id):
                raise ValueError(f"Unknown requirement id: {requirement_id}")
            if requirement_id in COMPILER_DERIVED_REQUIREMENTS:
                continue
            if requirement_id in existing:
                current = existing[requirement_id]
                current.label = requirement_label(requirement_id)
                current.kind = requirement_kind(requirement_id)
                current.required = _requirement_required_in_state(state, requirement_id, explicit=True)
                if "guidance" in data:
                    current.guidance = str(data.get("guidance") or "")
            else:
                requirement = Requirement(
                    id=requirement_id,
                    label=requirement_label(requirement_id),
                    kind=requirement_kind(requirement_id),
                    required=default_requirement_required(requirement_id),
                    guidance=str(data.get("guidance") or ""),
                )
                state.requirements.append(requirement)
                existing[requirement_id] = requirement

    def _ensure_requirements_for_evidence(self, state: CaseState, raw_items: list[Any]) -> None:
        existing = {item.id for item in state.requirements}
        support_ids = _support_requirement_ids(raw_items, existing=existing)
        derived_support_ids = False
        if not support_ids:
            support_ids = _support_requirement_ids_from_extracted_fields(raw_items)
            derived_support_ids = bool(support_ids)
        if not support_ids:
            return
        unknown_catalog_ids = sorted(requirement_id for requirement_id in support_ids if not is_known_requirement(requirement_id))
        if unknown_catalog_ids:
            raise ValueError(f"Evidence support references unknown requirements: {unknown_catalog_ids}")
        if _looks_like_wrong_ap_default(state, existing, support_ids):
            state.requirements = []
            existing = set()
        if existing:
            if _should_expand_ap_lite_requirements(state, existing, raw_items, support_ids):
                for requirement_id in AP_LITE_REQUIREMENTS:
                    if requirement_id in existing:
                        continue
                    state.requirements.append(
                        Requirement(
                            id=requirement_id,
                            label=requirement_label(requirement_id),
                            kind=requirement_kind(requirement_id),
                            required=default_requirement_required(requirement_id),
                        )
                    )
                    existing.add(requirement_id)
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
        ap_chain = _has_ap_chain_evidence(raw_items, support_ids)
        for requirement_id in requirement_ids:
            inferred_optional = (
                requirement_id in OPTIONAL_DYNAMIC_SUPPORT_REQUIREMENTS
                and requirement_id not in INVOICE_REQUIRED_FIELD_REQUIREMENTS
            )
            state.requirements.append(
                Requirement(
                    id=requirement_id,
                    label=requirement_label(requirement_id),
                    kind=requirement_kind(requirement_id),
                    required=False
                    if (ap_chain and requirement_id in INVOICE_FIELD_REQUIREMENTS) or inferred_optional
                    else default_requirement_required(requirement_id),
                )
            )

    def _append_evidence_updates(self, state: CaseState, raw_items: list[Any]) -> None:
        self._ensure_requirements_for_evidence(state, raw_items)
        used_ids = {item.id for item in state.evidence_items}
        for raw in raw_items:
            data = _normalize_evidence_data(raw)
            _normalize_evidence_supports_for_state(state, data)
            _normalize_evidence_metadata(data)
            _backfill_supports_from_extracted_fields(state, data)
            evidence_id = str(data.get("id") or self.next_evidence_id(state)).strip()
            if evidence_id in used_ids:
                raise ValueError(f"Duplicate evidence id: {evidence_id}")
            data["id"] = evidence_id
            if not data.get("created_at"):
                data["created_at"] = utc_now()
            state.evidence_items.append(EvidenceItem.model_validate(data))
            used_ids.add(evidence_id)

    def next_evidence_id(self, state: CaseState) -> str:
        used_ids = {item.id for item in state.evidence_items}
        index = len(used_ids) + 1
        while f"ev_{index:03d}" in used_ids:
            index += 1
        return f"ev_{index:03d}"

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
                    kind=requirement_kind(requirement_id),
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
        append_text(root / "traces" / "case_audit.jsonl", json.dumps(record, ensure_ascii=False) + "\n")

    def _refresh_requirements(self, state: CaseState) -> None:
        _canonicalize_requirement_definitions(state)
        _ensure_requirement_premises(state)
        link_manifest_evidence(self, state.case_id, state)
        trusted_sources = trusted_sources_for_evidence(self, state.case_id, state.evidence_items)
        trusted_evidence_ids = set(trusted_sources)
        _apply_supersession_metadata(state, trusted_evidence_ids)
        state.requirements = [
            item for item in state.requirements if item.id not in COMPILER_DERIVED_REQUIREMENTS
        ]
        active_evidence = _active_evidence_items(state, trusted_evidence_ids)
        compiled = compile_evidence_proof(
            active_evidence,
            state.verification_records,
            active_requirement_ids={_canonical_ap_requirement_id(item.id) for item in state.requirements},
            trusted_sources={item.id: trusted_sources[item.id] for item in active_evidence if item.id in trusted_sources},
        )
        state.compiled_proof = compiled if compiled.decisions else None
        compiled_requirements = {
            decision.requirement_id for decision in compiled.decisions
        }
        existing_requirements = {item.id for item in state.requirements}
        for requirement_id in sorted(compiled_requirements - existing_requirements):
            state.requirements.append(
                Requirement(
                    id=requirement_id,
                    label=requirement_label(requirement_id),
                    kind="cross_check",
                    required=True,
                )
            )
        evidence_by_requirement: dict[str, list[EvidenceItem]] = {}
        for item in active_evidence:
            if _is_advisory_memory_evidence(item):
                continue
            for support in item.supports:
                if support.requirement and support.requirement not in COMPILER_DERIVED_REQUIREMENTS:
                    evidence_by_requirement.setdefault(support.requirement, []).append(item)
        for req in state.requirements:
            if req.id in compiled_requirements and state.compiled_proof:
                decision = next(item for item in state.compiled_proof.decisions if item.requirement_id == req.id)
                final = next(
                    item
                    for item in state.compiled_proof.checks
                    if item.program_id == decision.program_id and item.id == decision.root_check_id
                )
                claim_evidence = {
                    claim.id: claim.evidence_id for claim in state.compiled_proof.claims
                }
                req.evidence_ids = _unique_strings(
                    [claim_evidence[claim_id] for claim_id in final.input_claim_ids if claim_id in claim_evidence]
                )
                req.kind = "cross_check"
                req.required = True
                req.status = {
                    "PROVED": "satisfied",
                    "DISPROVED": "conflict",
                    "INCOMPLETE": "weak" if req.evidence_ids else "missing",
                    "NOT_APPLICABLE": "weak",
                }[final.status]
                continue
            if req.id in REVIEWER_DERIVED_REQUIREMENTS:
                continue
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
        reportable_reviewer_conflicts = _project_reviewer_requirements(
            state,
            active_evidence,
            trusted_evidence_ids,
        )
        buckets = _material_buckets(state)
        state.missing_materials = buckets["missing_materials"]
        state.weak_materials = buckets["weak_materials"]
        state.conflict_materials = buckets["conflict_materials"]
        state.satisfied_materials = buckets["satisfied_materials"]
        reportable_proof_conflicts = {
            decision.requirement_id
            for decision in state.compiled_proof.decisions
            if decision.proof_status == "DISPROVED" and decision.outcome == "EVIDENCE_SUFFICIENT_FOR_REPORT"
        } if state.compiled_proof else set()
        reportable_conflicts = reportable_proof_conflicts | reportable_reviewer_conflicts
        blocking_conflicts = [item for item in state.conflict_materials if item not in reportable_conflicts]
        blockers = state.missing_materials + state.weak_materials + blocking_conflicts
        if blockers:
            state.status = "collecting_materials" if state.evidence_items else "new"
        elif state.evidence_items and state.requirements:
            state.status = "ready_for_report"
        elif state.evidence_items:
            state.status = "collecting_materials"


def _project_reviewer_requirements(
    state: CaseState,
    active_evidence: list[EvidenceItem],
    trusted_evidence_ids: set[str],
) -> set[str]:
    requirements = {item.id: item for item in state.requirements}
    acceptable = {
        item.id: item
        for item in active_evidence
        if item.id in trusted_evidence_ids
        and item.review_result.get("should_accept") is True
        and str(item.metadata.get("classification") or "business_evidence") == "business_evidence"
        and not _is_advisory_memory_evidence(item)
    }
    envelopes: dict[str, list[dict[str, Any]]] = {}
    for carrier in acceptable.values():
        for raw in carrier.metadata.get("requirement_verdicts") or []:
            if not isinstance(raw, dict):
                continue
            requirement_id = _normalize_requirement_id(raw.get("requirement_id"))
            if requirement_id in REVIEWER_DERIVED_REQUIREMENTS:
                envelopes.setdefault(requirement_id, []).append(raw)

    reportable: set[str] = set()
    for requirement_id in REVIEWER_DERIVED_REQUIREMENTS:
        requirement = requirements.get(requirement_id)
        if requirement is None:
            continue
        premises = [requirements.get(item) for item in requirement_premises(requirement_id)]
        if any(item is None or item.status not in {"accepted", "satisfied"} for item in premises):
            requirement.status = "weak"
            requirement.evidence_ids = []
            continue
        if requirement_unconfigured_policy_values(requirement_id):
            requirement.status = "weak"
            requirement.evidence_ids = []
            continue

        valid: list[tuple[str, list[str]]] = []
        for raw in envelopes.get(requirement_id, []):
            verdict = str(raw.get("verdict") or "").upper()
            raw_evidence_ids = raw.get("evidence_ids")
            open_questions = raw.get("open_questions")
            evidence_ids = _unique_strings(raw_evidence_ids) if isinstance(raw_evidence_ids, list) else []
            if (
                verdict not in {"SUPPORTED", "REFUTED", "UNKNOWN"}
                or str(raw.get("confidence") or "").lower() != "high"
                or not isinstance(open_questions, list)
                or open_questions
                or not str(raw.get("reason") or "").strip()
                or not evidence_ids
                or any(item not in acceptable for item in evidence_ids)
                or any(
                    not any(
                        any(
                            support.requirement == premise.id and support.support_level == "full"
                            for support in acceptable[evidence_id].supports
                        )
                        for evidence_id in evidence_ids
                    )
                    for premise in premises
                    if premise is not None
                )
            ):
                continue
            valid.append((verdict, evidence_ids))

        verdicts = {verdict for verdict, _ in valid}
        requirement.evidence_ids = _unique_strings([item for _, ids in valid for item in ids])
        if verdicts == {"SUPPORTED"}:
            requirement.status = "satisfied"
        elif verdicts == {"REFUTED"}:
            requirement.status = "conflict"
            reportable.add(requirement_id)
        else:
            requirement.status = "weak"
    return reportable


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


def _unique_conflicts(values: Any) -> list[ConflictRecord]:
    seen: set[str] = set()
    result: list[ConflictRecord] = []
    for value in values if isinstance(values, list) else [values]:
        conflict = ConflictRecord.model_validate(value)
        key = conflict.model_dump_json(exclude_none=True)
        if key not in seen:
            seen.add(key)
            result.append(conflict)
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


def _active_evidence_items(state: CaseState, trusted_evidence_ids: set[str]) -> list[EvidenceItem]:
    superseded = _superseded_evidence_ids(state.evidence_items, trusted_evidence_ids)
    return [item for item in state.evidence_items if item.id not in superseded]


def _is_advisory_memory_evidence(item: EvidenceItem) -> bool:
    metadata = item.metadata if isinstance(item.metadata, dict) else {}
    review_result = item.review_result if isinstance(item.review_result, dict) else {}
    boundary = str(metadata.get("boundary") or review_result.get("boundary") or "").lower()
    truth_status = str(metadata.get("truth_status") or review_result.get("truth_status") or "").lower()
    source_ref = str(metadata.get("source_ref") or review_result.get("source_ref") or "").lower()
    if item.source == "rag":
        return True
    if boundary == "memory_hint_only_not_case_truth":
        return True
    if truth_status == "advisory":
        return True
    return "memory_hint_only_not_case_truth" in source_ref


def _superseded_evidence_ids(items: list[EvidenceItem], trusted_evidence_ids: set[str]) -> set[str]:
    return set(_supersession_map(items, trusted_evidence_ids))


def _apply_supersession_metadata(state: CaseState, trusted_evidence_ids: set[str]) -> bool:
    superseded_by = _supersession_map(state.evidence_items, trusted_evidence_ids)
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


def _supersession_map(items: list[EvidenceItem], trusted_evidence_ids: set[str]) -> dict[str, str]:
    counts = Counter(item.id for item in items)
    by_id = {item.id: item for item in items if counts[item.id] == 1}
    candidates: dict[str, list[str]] = {}
    for item in items:
        if counts[item.id] != 1 or not _can_supersede_evidence(item, trusted_evidence_ids):
            continue
        for old_id in _metadata_supersedes_ids(item.metadata):
            old = by_id.get(old_id)
            if old is not None and old.id != item.id and old.type == item.type:
                candidates.setdefault(old_id, []).append(item.id)
    return {old_id: replacements[0] for old_id, replacements in candidates.items() if len(replacements) == 1}


def _can_supersede_evidence(item: EvidenceItem, trusted_evidence_ids: set[str]) -> bool:
    if item.id not in trusted_evidence_ids or item.source != "attachment" or item.credibility == "low":
        return False
    review_result = item.review_result if isinstance(item.review_result, dict) else {}
    metadata = item.metadata if isinstance(item.metadata, dict) else {}
    if review_result.get("should_accept") is not True or _is_advisory_memory_evidence(item):
        return False
    if str(metadata.get("classification") or "").lower() != "business_evidence":
        return False
    if str(metadata.get("review_stage") or "").lower() != "corrected":
        return False
    return bool(_metadata_supersedes_ids(metadata))


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
    data["conflicts"] = _unique_conflicts(data.get("conflicts") or [])
    data["quoted_text"] = _string_list(data.get("quoted_text"))
    data["supports"] = _normalize_support_records(data.get("supports"))
    if _is_cross_case_evidence(data):
        data["supports"] = []
        data["conflicts"] = _unique_conflicts(
            data["conflicts"] + ["Cross-case or mixed-case evidence is recorded but not linked to active requirements."]
        )
    if _is_prompt_injection_evidence(data):
        _quarantine_prompt_injection_evidence(data)
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
    data["conflicts"] = _unique_conflicts(["prompt_injection_quarantine"])
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


def _normalize_evidence_supports_for_state(state: CaseState, data: dict[str, Any]) -> None:
    existing = {item.id for item in state.requirements}
    supports = data.get("supports")
    if not isinstance(supports, list):
        return
    supports = [
        support
        for support in supports
        if not (
            isinstance(support, dict)
            and _normalize_requirement_id(support.get("requirement"))
            in COMPILER_DERIVED_REQUIREMENTS | REVIEWER_DERIVED_REQUIREMENTS
        )
    ]
    data["supports"] = supports
    if not existing:
        return
    for support in supports:
        if not isinstance(support, dict):
            continue
        support["requirement"] = _support_requirement_id_for_existing(support.get("requirement"), existing)


def _support_requirement_id_for_existing(value: Any, existing: set[str]) -> str:
    requirement_id = _normalize_requirement_id(value)
    alias = SUPPORT_REQUIREMENT_ID_ALIASES.get(requirement_id, requirement_id)
    if alias in existing and requirement_id not in existing:
        return alias
    return requirement_id


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
    review = data.get("review_result") if isinstance(data.get("review_result"), dict) else {}
    if not bool(review.get("should_accept")):
        return
    requirement_id = _core_requirement_for_evidence(data)
    if not requirement_id:
        return
    if any(
        _conflict_mentions_requirement(conflict, requirement_id) and not resolved_conflict_note(conflict)
        for conflict in data.get("conflicts") or []
    ):
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


def _support_requirement_ids(raw_items: list[Any], *, existing: set[str] | None = None) -> list[str]:
    result: list[str] = []
    existing_ids = existing or set()
    for raw in raw_items:
        data = raw.model_dump(exclude_none=True) if hasattr(raw, "model_dump") else dict(raw or {})
        for support in data.get("supports") or []:
            support_data = support.model_dump(exclude_none=True) if hasattr(support, "model_dump") else dict(support or {})
            requirement_id = (
                _support_requirement_id_for_existing(support_data.get("requirement"), existing_ids)
                if existing_ids
                else _normalize_requirement_id(support_data.get("requirement"))
            )
            if requirement_id and requirement_id not in COMPILER_DERIVED_REQUIREMENTS:
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
    existing_supports = {
        _normalize_requirement_id(support.get("requirement")): support
        for support in supports
        if isinstance(support, dict)
    }
    metadata = _metadata_dict(data)
    for requirement in state.requirements:
        source = _field_support_source(requirement.id, fields, metadata)
        if not source:
            continue
        quote, level = source
        existing = existing_supports.get(requirement.id)
        if existing:
            if level == "full" and existing.get("support_level") in {"none", "partial"}:
                existing["support_level"] = "full"
                existing["quoted_text"] = existing.get("quoted_text") or quote
            continue
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
            if not field_id:
                continue
            current = result.get(field_id)
            result[field_id] = {**row, **current} if isinstance(current, dict) else row
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
    if _has_ap_chain_evidence(raw_items, support_ids):
        canonical_supports = [_canonical_ap_requirement_id(item) for item in support_ids]
        return _unique_strings(list(INVOICE_FIELD_REQUIREMENTS) + list(AP_LITE_REQUIREMENTS) + canonical_supports)
    if has_invoice_evidence or any(item in INVOICE_FIELD_REQUIREMENTS for item in support_ids):
        return _unique_strings(list(INVOICE_FIELD_REQUIREMENTS) + list(support_ids))
    return support_ids


def _has_ap_chain_evidence(raw_items: list[Any], support_ids: list[str]) -> bool:
    ap_ids = _ap_requirement_ids(raw_items, support_ids)
    return "invoice" in ap_ids and len(ap_ids) >= 3


def _should_expand_ap_lite_requirements(
    state: CaseState,
    existing: set[str],
    raw_items: list[Any],
    support_ids: list[str],
) -> bool:
    if not existing:
        return False
    ap_ids = _ap_requirement_ids(raw_items, support_ids, state=state)
    if "invoice" not in ap_ids or len(ap_ids) < 3:
        return False
    return bool(
        (existing & set(INVOICE_FIELD_REQUIREMENTS))
        or (existing & set(AP_LITE_REQUIREMENTS))
        or (existing & set(AP_THREE_WAY_REQUIREMENTS))
    )


def _ap_requirement_ids(
    raw_items: list[Any],
    support_ids: list[str],
    *,
    state: CaseState | None = None,
) -> set[str]:
    result = {_canonical_ap_requirement_id(item) for item in support_ids}
    for raw in raw_items:
        data = raw.model_dump(exclude_none=True) if hasattr(raw, "model_dump") else dict(raw or {})
        result.add(_canonical_ap_requirement_id(_core_requirement_for_evidence(data)))
    if state is not None:
        for item in state.evidence_items:
            result.add(_canonical_ap_requirement_id(_core_requirement_for_evidence(item.model_dump())))
            for support in item.supports:
                result.add(_canonical_ap_requirement_id(support.requirement))
    return {item for item in result if item in set(AP_LITE_REQUIREMENTS)}


def _canonical_ap_requirement_id(value: Any) -> str:
    requirement_id = _normalize_requirement_id(value)
    return SUPPORT_REQUIREMENT_ID_ALIASES.get(requirement_id, requirement_id)


def _looks_like_wrong_ap_default(state: CaseState, existing: set[str], support_ids: list[str]) -> bool:
    if state.evidence_items:
        return False
    ap_defaults = set(AP_THREE_WAY_REQUIREMENTS) | set(AP_LITE_REQUIREMENTS)
    if not existing or not existing.issubset(ap_defaults):
        return False
    has_invoice_field_support = any(item in INVOICE_FIELD_REQUIREMENTS for item in support_ids)
    has_ap_support = any(_canonical_ap_requirement_id(item) in AP_LITE_REQUIREMENTS for item in support_ids)
    return has_invoice_field_support and not has_ap_support


def _requirement_has_evidence(state: CaseState, requirement_id: str) -> bool:
    for item in state.evidence_items:
        for support in item.supports:
            if support.requirement == requirement_id:
                return True
    return any(requirement_id in item.evidence_ids for item in state.requirements if item.id == requirement_id)


def _canonicalize_requirement_definitions(state: CaseState) -> None:
    for requirement in state.requirements:
        if not is_known_requirement(requirement.id):
            continue
        requirement.label = requirement_label(requirement.id)
        requirement.kind = requirement_kind(requirement.id)
        requirement.required = _requirement_required_in_state(
            state,
            requirement.id,
            current_required=requirement.required,
        )


def _ensure_requirement_premises(state: CaseState) -> None:
    existing = {item.id: item for item in state.requirements}
    for conclusion in list(state.requirements):
        for requirement_id in requirement_premises(conclusion.id):
            current = existing.get(requirement_id)
            if current is not None:
                if conclusion.required:
                    current.required = True
                continue
            current = Requirement(
                id=requirement_id,
                label=requirement_label(requirement_id),
                kind=requirement_kind(requirement_id),
                required=conclusion.required,
            )
            state.requirements.append(current)
            existing[requirement_id] = current


def _requirement_required_in_state(
    state: CaseState,
    requirement_id: str,
    *,
    explicit: bool = False,
    current_required: bool | None = None,
) -> bool:
    active_ids = {item.id for item in state.requirements}
    ap_ids = active_ids.intersection(AP_LITE_REQUIREMENTS)
    if requirement_id in INVOICE_FIELD_REQUIREMENTS and "invoice" in ap_ids and len(ap_ids) >= 3:
        return False
    if any(item.required and requirement_id in requirement_premises(item.id) for item in state.requirements):
        return True
    if explicit:
        return default_requirement_required(requirement_id)
    optional_premise = any(
        not item.required and requirement_id in requirement_premises(item.id)
        for item in state.requirements
    )
    if current_required is False and (
        requirement_id in OPTIONAL_DYNAMIC_SUPPORT_REQUIREMENTS or optional_premise
    ):
        return False
    return default_requirement_required(requirement_id)


def _jsonish_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if hasattr(value, "model_dump"):
        value = value.model_dump(exclude_none=True)
    return json.dumps(value, ensure_ascii=False, default=str)


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
        _conflict_mentions_requirement(conflict, requirement_id) and not resolved_conflict_note(conflict)
        for conflict in conflicts
    )


def _conflict_mentions_requirement(conflict: Any, requirement_id: str) -> bool:
    data = _conflict_data(conflict)
    expected = SUPPORT_REQUIREMENT_ID_ALIASES.get(requirement_id, requirement_id)
    if isinstance(data, dict):
        explicit = _safe_requirement_id(data.get("requirement"))
        if explicit:
            return SUPPORT_REQUIREMENT_ID_ALIASES.get(explicit, explicit) == expected
        affected = data.get("affected_requirements") or data.get("affected_fields") or []
        affected_ids = {
            SUPPORT_REQUIREMENT_ID_ALIASES.get(item, item)
            for value in (affected if isinstance(affected, list) else [affected])
            if (item := _safe_requirement_id(value))
        }
        if affected_ids:
            return expected in affected_ids
    return False
