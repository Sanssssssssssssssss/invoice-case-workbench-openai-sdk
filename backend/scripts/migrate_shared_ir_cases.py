from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.domain.invoice_contracts import build_requirement_contracts, contract_hole_id
from app.domain.invoice_requirements import REQUIREMENT_PACK
from app.state.case_store import CaseStore, _migrate_case_state_data
from app.state.schemas import CaseState, SemanticClaimCandidate, SemanticProposalCandidate


LEGACY_CLAIM_KEY = "claim_to_source_refs"
LEGACY_PROPOSAL_KEYS = ("semantic_judgments", "requirement_verdicts", "proof_proposals")
LEGACY_KEYS = (LEGACY_CLAIM_KEY, *LEGACY_PROPOSAL_KEYS)
TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm"}
TRUSTED_MANIFEST_STATUSES = {"active", "weak"}
STRONG_VERDICTS = {"SUPPORTED", "REFUTED"}


def _now_slug() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalized(value: Any) -> str:
    return " ".join(str(value or "").split())


def _rows(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _case_files(workspace_root: Path) -> list[Path]:
    active = list(workspace_root.glob("*/case_state.json"))
    archived = list((workspace_root / ".archived_cases").glob("*/case_state.json"))
    return sorted({path.resolve() for path in (*active, *archived)})


def _legacy_compiled_proof(value: Any) -> bool:
    return isinstance(value, dict) and bool(value) and not {
        "evidence_ir",
        "contracts",
        "decisions",
    }.issubset(value)


def _legacy_present(document: dict[str, Any]) -> bool:
    if _legacy_compiled_proof(document.get("compiled_proof")):
        return True
    return any(
        key in metadata
        for evidence in _rows(document.get("evidence_items"))
        for metadata in [evidence.get("metadata")]
        if isinstance(metadata, dict)
        for key in LEGACY_KEYS
    )


def _trusted_texts(case_dir: Path, document: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    manifest_path = case_dir / "attachments" / "attachment_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = [item for item in manifest.get("attachments", []) if isinstance(item, dict)]
    result: dict[str, tuple[str, ...]] = {}
    for evidence in _rows(document.get("evidence_items")):
        evidence_id = str(evidence.get("id") or "").strip()
        metadata = evidence.get("metadata") if isinstance(evidence.get("metadata"), dict) else {}
        review = evidence.get("review_result") if isinstance(evidence.get("review_result"), dict) else {}
        identity = {
            "attachment_id": str(metadata.get("attachment_id") or ""),
            "original_ref": str(metadata.get("original_ref") or ""),
            "name": str(metadata.get("source_filename") or ""),
        }
        matches = [
            item
            for item in entries
            if any(identity.values())
            and all(not value or str(item.get(key) or "") == value for key, value in identity.items())
        ]
        classification = str(metadata.get("classification") or "").lower()
        if (
            not evidence_id
            or str(evidence.get("source") or "") != "attachment"
            or str(evidence.get("credibility") or "medium") == "low"
            or review.get("should_accept") is not True
            or classification != "business_evidence"
            or len(matches) != 1
        ):
            continue
        entry = matches[0]
        original_ref = str(entry.get("original_ref") or "")
        expected_sha = str(entry.get("sha256") or "")
        original = (case_dir / original_ref).resolve()
        if (
            str(entry.get("status") or "") not in TRUSTED_MANIFEST_STATUSES
            or not original_ref
            or not expected_sha
            or case_dir.resolve() not in original.parents
            or not original.is_file()
            or hashlib.sha256(original.read_bytes()).hexdigest() != expected_sha
        ):
            continue
        texts: list[str] = []
        extraction_ref = str(entry.get("extraction_ref") or "")
        extraction_sha = str(entry.get("extraction_sha256") or "")
        if extraction_ref and extraction_sha:
            dossier_path = (case_dir / extraction_ref).resolve()
            if (
                case_dir.resolve() in dossier_path.parents
                and dossier_path.is_file()
                and hashlib.sha256(dossier_path.read_bytes()).hexdigest() == extraction_sha
            ):
                try:
                    dossier = json.loads(dossier_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    dossier = {}
                if str(dossier.get("attachment_id") or "") == str(entry.get("attachment_id") or ""):
                    texts.extend(str(dossier.get(key) or "") for key in ("full_text", "body_markdown"))
                    for key, field in (
                        ("pages", "text"),
                        ("blocks", "text"),
                        ("tables", "markdown"),
                        ("tables", "csv"),
                        ("field_inventory", "source_quote"),
                        ("block_crops", "text"),
                    ):
                        texts.extend(
                            str(row.get(field) or "")
                            for row in dossier.get(key) or []
                            if isinstance(row, dict)
                        )
        if original.suffix.lower() in TEXT_SUFFIXES:
            texts.append(original.read_text(encoding="utf-8", errors="replace"))
        compact = tuple(dict.fromkeys(text for text in texts if text))
        if compact:
            result[evidence_id] = compact
    return result


def _grounded(texts: dict[str, tuple[str, ...]], evidence_id: str, quote: str) -> bool:
    expected = _normalized(quote)
    return bool(expected) and any(expected in _normalized(text) for text in texts.get(evidence_id, ()))


def _contract_rows(document: dict[str, Any]) -> tuple[list[Any], list[str]]:
    known = set((REQUIREMENT_PACK.get("requirements") or {}).keys())
    requirement_ids = [
        str(item.get("id") or "").strip()
        for item in _rows(document.get("requirements"))
        if str(item.get("id") or "").strip()
    ]
    unknown = sorted(set(requirement_ids) - known)
    contracts, _holes = build_requirement_contracts(item for item in requirement_ids if item in known)
    return contracts, unknown


def _supports(evidence: dict[str, Any]) -> set[str]:
    return {
        str(row.get("requirement") or "")
        for row in _rows(evidence.get("supports"))
        if str(row.get("support_level") or "none") != "none"
    }


def _claim_candidate(
    row: dict[str, Any],
    evidence: dict[str, Any],
    index: int,
    contracts: list[Any],
    trusted_texts: dict[str, tuple[str, ...]],
) -> tuple[dict[str, Any] | None, str]:
    evidence_id = str(evidence.get("id") or "").strip()
    required = ("subject", "predicate", "value_type")
    if any(not str(row.get(key) or "").strip() for key in required) or not any(
        key in row for key in ("typed_value", "value")
    ):
        return None, "incomplete_claim_shape"
    quote = str(row.get("source_quote") or row.get("quote") or "").strip()
    locator = str(row.get("source_locator") or row.get("locator") or "").strip()
    confidence = str(row.get("confidence") or "low").lower()
    if not quote or not locator or confidence not in {"medium", "high"}:
        return None, "incomplete_source_reference"
    if not _grounded(trusted_texts, evidence_id, quote):
        return None, "source_unbound_or_quote_not_grounded"
    subject = str(row["subject"]).strip()
    predicate = str(row["predicate"]).strip()
    value_type = str(row["value_type"]).strip().lower()
    supported = _supports(evidence)
    matches = [
        (contract_hole_id(input_, contract), input_)
        for contract in contracts
        for input_ in contract.inputs
        if input_.hole_kind in {"claim", "relation"}
        and input_.subject == subject
        and input_.predicate == predicate
        and input_.value_type == value_type
        and (not input_.role or input_.role in supported)
    ]
    by_hole = {hole_id: input_ for hole_id, input_ in matches}
    if len(by_hole) != 1:
        return None, "claim_slot_unmapped_or_ambiguous"
    hole_id, input_ = next(iter(by_hole.items()))
    attributes = dict(row.get("attributes")) if isinstance(row.get("attributes"), dict) else {}
    for key in ("unit", "currency", "basis", "tax_basis", "coverage"):
        if row.get(key) not in {None, ""}:
            attributes.setdefault(key, row[key])
    if set(input_.required_attributes) - set(attributes):
        return None, "required_attributes_missing"
    attribute_sources = row.get("attribute_sources") if isinstance(row.get("attribute_sources"), dict) else {}
    if any(
        not isinstance(attribute_sources.get(key), dict)
        or not str(attribute_sources[key].get("source_quote") or "").strip()
        or not str(attribute_sources[key].get("source_locator") or "").strip()
        or not _grounded(trusted_texts, evidence_id, str(attribute_sources[key].get("source_quote") or ""))
        for key in input_.required_attributes
    ):
        return None, "required_attribute_sources_missing"
    handle = "legacy_claim_" + _hash({
        "evidence_id": evidence_id,
        "index": index,
        "subject": subject,
        "predicate": predicate,
        "quote": quote,
        "locator": locator,
    })[:20]
    candidate = SemanticClaimCandidate.model_validate({
        "handle": handle,
        "hole_id": hole_id,
        "typed_value": row.get("typed_value", row.get("value")),
        "source_quote": quote,
        "source_locator": locator,
        "confidence": confidence,
        "entity_handle": str(row.get("entity_handle") or row.get("entity_key") or ""),
        "attributes": attributes,
        "attribute_sources": attribute_sources,
    })
    return candidate.model_dump(mode="json"), ""


def _resolve_claim_refs(refs: Any, records: list[dict[str, str]]) -> tuple[list[str], bool]:
    raw_refs = _rows(refs)
    if not raw_refs:
        return [], False
    result: list[str] = []
    for ref in raw_refs:
        evidence_id = str(ref.get("evidence_id") or "").strip()
        claim_id = str(ref.get("claim_id") or ref.get("id") or "").strip()
        subject = str(ref.get("subject") or "").strip()
        predicate = str(ref.get("predicate") or ref.get("field") or "").strip()
        quote = _normalized(ref.get("source_quote") or ref.get("quote"))
        locator = str(ref.get("source_locator") or ref.get("locator") or "").strip()
        matches = [
            item
            for item in records
            if (not evidence_id or item["evidence_id"] == evidence_id)
            and (not claim_id or item["old_id"] == claim_id)
            and (not subject or item["subject"] == subject)
            and (not predicate or item["predicate"] == predicate)
            and (not quote or _normalized(item["quote"]) == quote)
            and (not locator or item["locator"] == locator)
        ]
        if len(matches) != 1:
            return [], False
        result.append(matches[0]["handle"])
    return list(dict.fromkeys(result)), True


def _proposal_candidate(
    row: dict[str, Any],
    evidence: dict[str, Any],
    index: int,
    contracts: list[Any],
    claim_records: list[dict[str, str]],
    trusted_texts: dict[str, tuple[str, ...]],
) -> tuple[dict[str, Any] | None, str, bool]:
    evidence_id = str(evidence.get("id") or "").strip()
    verdict = str(row.get("verdict") or "UNKNOWN").upper()
    strong = verdict in STRONG_VERDICTS
    if evidence_id not in trusted_texts:
        return None, "proposal_source_unbound", strong
    contract_id = str(row.get("contract_id") or "").strip()
    requirement_id = str(row.get("requirement_id") or row.get("requirement") or "").strip()
    target = str(row.get("target_predicate") or "").strip()
    matches = [
        contract
        for contract in contracts
        if (not contract_id or contract.contract_id == contract_id)
        and (not requirement_id or contract.requirement_id == requirement_id)
        and (not target or contract.target_predicate == target)
        and any(input_.hole_kind == "judgment" for input_ in contract.inputs)
    ]
    if not any((contract_id, requirement_id, target)) or len(matches) != 1:
        return None, "proposal_contract_unmapped_or_ambiguous", strong
    contract = matches[0]
    judgments = [input_ for input_ in contract.inputs if input_.hole_kind == "judgment"]
    if len(judgments) != 1 or verdict not in {"SUPPORTED", "REFUTED", "UNKNOWN"}:
        return None, "proposal_shape_invalid", strong
    input_handles, inputs_ok = _resolve_claim_refs(row.get("input_refs") or row.get("considered_refs"), claim_records)
    supporting, supporting_ok = _resolve_claim_refs(row.get("supporting_refs"), claim_records)
    opposing, opposing_ok = _resolve_claim_refs(row.get("opposing_refs"), claim_records)
    questions = [str(value).strip() for value in row.get("open_questions") or [] if str(value).strip()]
    confidence = str(row.get("confidence") or "low").lower()
    if not inputs_ok or not input_handles:
        return None, "proposal_refs_unresolved", strong
    if strong and (
        confidence != "high"
        or questions
        or verdict == "SUPPORTED" and (not supporting_ok or set(supporting) != set(input_handles) or opposing)
        or verdict == "REFUTED" and (not opposing_ok or not opposing)
    ):
        return None, "strong_proposal_not_fully_grounded", True
    candidate = SemanticProposalCandidate.model_validate({
        "handle": "legacy_proposal_" + _hash({"evidence_id": evidence_id, "index": index, "row": row})[:20],
        "hole_id": contract_hole_id(judgments[0], contract),
        "verdict": verdict,
        "input_handles": input_handles,
        "supporting_handles": supporting,
        "opposing_handles": opposing,
        "entity_handle": str(row.get("entity_handle") or ""),
        "open_questions": questions,
        "confidence": confidence,
        "reason": str(row.get("reason") or "").strip(),
    })
    return candidate.model_dump(mode="json"), "", strong


def migrate_document(
    document: dict[str, Any],
    *,
    trusted_texts: dict[str, tuple[str, ...]],
) -> tuple[dict[str, Any], Counter[str], dict[str, int]]:
    result = copy.deepcopy(document)
    stats: Counter[str] = Counter(files_scanned=1)
    skipped: Counter[str] = Counter()
    if not _legacy_present(result):
        return result, stats, dict(skipped)
    stats["cases_planned"] = 1
    contracts, unknown = _contract_rows(result)
    stats["unknown_requirement_ids"] += len(unknown)
    evidence_rows = _rows(result.get("evidence_items"))
    claim_records: list[dict[str, str]] = []
    for evidence in evidence_rows:
        metadata = evidence.get("metadata") if isinstance(evidence.get("metadata"), dict) else {}
        old_claims = _rows(metadata.get(LEGACY_CLAIM_KEY))
        stats["legacy_claim_rows"] += len(old_claims)
        existing = _rows(evidence.get("semantic_claims"))
        for index, row in enumerate(old_claims):
            candidate, reason = _claim_candidate(row, evidence, index, contracts, trusted_texts)
            if candidate is None:
                skipped[reason] += 1
                stats["claim_rows_skipped"] += 1
                continue
            existing.append(candidate)
            stats["claim_rows_migrated"] += 1
            claim_records.append({
                "handle": candidate["handle"],
                "evidence_id": str(evidence.get("id") or ""),
                "old_id": str(row.get("id") or row.get("claim_id") or ""),
                "subject": str(row.get("subject") or ""),
                "predicate": str(row.get("predicate") or ""),
                "quote": str(row.get("source_quote") or row.get("quote") or ""),
                "locator": str(row.get("source_locator") or row.get("locator") or ""),
            })
        if existing:
            evidence["semantic_claims"] = existing
    for evidence in evidence_rows:
        metadata = evidence.get("metadata") if isinstance(evidence.get("metadata"), dict) else {}
        existing = _rows(evidence.get("semantic_proposals"))
        proposal_index = 0
        for key in LEGACY_PROPOSAL_KEYS:
            old_rows = _rows(metadata.get(key))
            stats[f"legacy_{key}_rows"] += len(old_rows)
            for row in old_rows:
                candidate, reason, strong = _proposal_candidate(
                    row,
                    evidence,
                    proposal_index,
                    contracts,
                    claim_records,
                    trusted_texts,
                )
                proposal_index += 1
                if candidate is None:
                    skipped[reason] += 1
                    stats["proposal_rows_skipped"] += 1
                    stats["strong_conclusions_downgraded"] += int(strong)
                    continue
                existing.append(candidate)
                stats["proposal_rows_migrated"] += 1
        if existing:
            evidence["semantic_proposals"] = existing
        for key in LEGACY_KEYS:
            metadata.pop(key, None)
        evidence["metadata"] = metadata
    if _legacy_compiled_proof(result.get("compiled_proof")):
        stats["legacy_compiled_proofs_removed"] += 1
    result["compiled_proof"] = None
    return result, stats, dict(skipped)


def _status_map(document: dict[str, Any]) -> dict[str, str]:
    return {
        str(item.get("id") or ""): str(item.get("status") or "")
        for item in _rows(document.get("requirements"))
    }


def _fallback_degrade(document: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(document)
    compiler_owned = {
        key
        for key, value in (REQUIREMENT_PACK.get("requirements") or {}).items()
        if value.get("owner") != "evidence"
    }
    for requirement in _rows(result.get("requirements")):
        if str(requirement.get("id") or "") in compiler_owned:
            requirement["status"] = "missing"
            requirement["evidence_ids"] = []
    result["compiled_proof"] = None
    result["status"] = "collecting_materials" if result.get("evidence_items") else "new"
    result["missing_materials"] = [
        str(item.get("id") or "")
        for item in _rows(result.get("requirements"))
        if item.get("required", True) and item.get("status") == "missing"
    ]
    result["weak_materials"] = []
    result["conflict_materials"] = []
    result["satisfied_materials"] = [
        str(item.get("id") or "")
        for item in _rows(result.get("requirements"))
        if item.get("status") in {"accepted", "satisfied"}
    ]
    return result


def _recompile(case_path: Path, document: dict[str, Any]) -> tuple[dict[str, Any], str]:
    store = CaseStore(case_path.parent.parent)
    state = CaseState.model_validate(_migrate_case_state_data(document))
    persisted_case_id = state.case_id
    state.case_id = case_path.parent.name
    try:
        store._refresh_requirements(state)
    except Exception as exc:  # fail closed; the report retains the concrete error
        return _fallback_degrade(document), f"{type(exc).__name__}: {exc}"
    finally:
        state.case_id = persisted_case_id
    return state.model_dump(mode="json"), ""


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".migration_tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def _write_json(path: Path, value: Any) -> None:
    _atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n").encode("utf-8"))


def _backup_files(workspace_root: Path, targets: list[Path], backup_dir: Path) -> dict[str, Any]:
    if backup_dir.exists() and any(backup_dir.iterdir()):
        raise FileExistsError(f"Backup directory is not empty: {backup_dir}")
    files = []
    for target in sorted(set(path.resolve() for path in targets if path.is_file())):
        relative = target.relative_to(workspace_root.resolve())
        destination = backup_dir / "files" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, destination)
        files.append({
            "target": relative.as_posix(),
            "backup": destination.relative_to(backup_dir).as_posix(),
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        })
    manifest = {
        "version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "workspace_root": str(workspace_root.resolve()),
        "files": files,
    }
    _write_json(backup_dir / "backup_manifest.json", manifest)
    return manifest


def restore_backup(backup_dir: Path) -> dict[str, Any]:
    manifest = json.loads((backup_dir / "backup_manifest.json").read_text(encoding="utf-8"))
    workspace_root = Path(manifest["workspace_root"]).resolve()
    restored = 0
    for row in manifest.get("files") or []:
        target = (workspace_root / str(row["target"])).resolve()
        source = (backup_dir / str(row["backup"])).resolve()
        if workspace_root not in target.parents or backup_dir.resolve() not in source.parents:
            raise ValueError("Backup manifest path escapes its declared root")
        payload = source.read_bytes()
        if hashlib.sha256(payload).hexdigest() != str(row["sha256"]):
            raise ValueError(f"Backup checksum mismatch: {source}")
        _atomic_write(target, payload)
        restored += 1
    return {"restored_files": restored, "backup_dir": str(backup_dir.resolve())}


def _session_inventory(db_path: Path, workspace_root: Path) -> dict[str, Any]:
    if not db_path.is_file():
        return {"path": str(db_path.resolve()), "exists": False}
    connection = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        sessions = int(connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
        items = int(connection.execute("SELECT COUNT(*) FROM session_items").fetchone()[0])
        case_ids = [str(row[0]) for row in connection.execute("SELECT case_id FROM sessions")]
        columns = [str(row[1]) for row in connection.execute("PRAGMA table_info(session_items)")]
    finally:
        connection.close()
    return {
        "path": str(db_path.resolve()),
        "exists": True,
        "sessions": sessions,
        "session_items": items,
        "session_case_ids": len(set(case_ids)),
        "without_active_case_file": sum(
            1 for case_id in set(case_ids) if not (workspace_root / case_id / "case_state.json").is_file()
        ),
        "business_state_columns": sorted(set(columns) & {
            "evidence_items", "compiled_proof", "claim_to_source_refs", "semantic_judgments"
        }),
        "mutated": False,
    }


def run_migration(
    *,
    workspace_root: Path,
    session_db: Path,
    apply: bool,
    backup_dir: Path | None = None,
) -> dict[str, Any]:
    workspace_root = workspace_root.resolve()
    plans: list[tuple[Path, dict[str, Any], dict[str, Any], Counter[str], dict[str, int]]] = []
    totals: Counter[str] = Counter()
    skip_totals: Counter[str] = Counter()
    files = _case_files(workspace_root)
    for path in files:
        original = json.loads(path.read_text(encoding="utf-8"))
        transformed, stats, skipped = migrate_document(
            original,
            trusted_texts=_trusted_texts(path.parent, original),
        )
        totals.update(stats)
        skip_totals.update(skipped)
        if stats["cases_planned"]:
            plans.append((path, original, transformed, stats, skipped))
    report: dict[str, Any] = {
        "mode": "apply" if apply else "dry-run",
        "created_at": datetime.now(UTC).isoformat(),
        "workspace_root": str(workspace_root),
        "case_state_files": len(files),
        "active_case_state_files": sum(".archived_cases" not in path.parts for path in files),
        "archived_case_state_files": sum(".archived_cases" in path.parts for path in files),
        "counts": dict(sorted(totals.items())),
        "skipped_by_reason": dict(sorted(skip_totals.items())),
        "session_store": _session_inventory(session_db, workspace_root),
        "changed_cases": [],
    }
    if not apply:
        return report
    if backup_dir is None:
        raise ValueError("--backup-dir is required with --apply")
    targets = [item[0] for item in plans]
    targets.extend(path.parent / "attachments" / "attachment_manifest.json" for path, *_ in plans)
    backup = _backup_files(workspace_root, targets, backup_dir.resolve())
    report["backup_dir"] = str(backup_dir.resolve())
    report["backup_files"] = len(backup["files"])
    report["backup_case_state_files"] = sum(
        Path(item["target"]).name == "case_state.json" for item in backup["files"]
    )
    report["backup_attachment_manifests"] = sum(
        Path(item["target"]).name == "attachment_manifest.json" for item in backup["files"]
    )
    for path, original, transformed, _stats, _skipped in plans:
        before_status = _status_map(original)
        manifest_path = path.parent / "attachments" / "attachment_manifest.json"
        manifest_before = hashlib.sha256(manifest_path.read_bytes()).hexdigest() if manifest_path.is_file() else ""
        compiled, error = _recompile(path, transformed)
        manifest_after = hashlib.sha256(manifest_path.read_bytes()).hexdigest() if manifest_path.is_file() else ""
        totals["attachment_manifests_updated"] += int(manifest_before != manifest_after)
        after_status = _status_map(compiled)
        downgraded = sorted(
            key
            for key, value in before_status.items()
            if value in {"accepted", "satisfied", "conflict"}
            and after_status.get(key) in {"missing", "weak"}
        )
        totals["strong_requirement_statuses_downgraded"] += len(downgraded)
        totals["cases_recompiled"] += int(not error)
        totals["cases_recompile_failed_closed"] += int(bool(error))
        _write_json(path, compiled)
        report["changed_cases"].append({
            "path": path.relative_to(workspace_root).as_posix(),
            "downgraded_requirements": downgraded,
            "recompile_error": error,
        })
    report["counts"] = dict(sorted(totals.items()))
    report["changed_case_files"] = len(plans)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate legacy case semantics into the shared Evidence IR input protocol.")
    parser.add_argument("--workspace-root", type=Path, default=REPO_ROOT / "workspace" / "cases")
    parser.add_argument("--session-db", type=Path, default=BACKEND_ROOT / "storage" / "sessions.sqlite")
    parser.add_argument("--apply", action="store_true", help="Write backups and migrate. The default is dry-run.")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--restore", type=Path, help="Restore every file listed by a previous backup manifest.")
    args = parser.parse_args()
    if args.restore:
        report = restore_backup(args.restore.resolve())
    else:
        backup_dir = args.backup_dir
        if args.apply and backup_dir is None:
            backup_dir = REPO_ROOT / "workspace" / "migration_backups" / f"shared_ir_{_now_slug()}"
        report = run_migration(
            workspace_root=args.workspace_root,
            session_db=args.session_db,
            apply=args.apply,
            backup_dir=backup_dir,
        )
    if args.report:
        _write_json(args.report.resolve(), report)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
