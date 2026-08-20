from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


TRISTATES = {"SUPPORTED", "CONTRADICTED", "NOT_FOUND"}
INTERNAL_PROMPT_TOKENS = (
    "SUPPORTED",
    "CONTRADICTED",
    "NOT_FOUND",
    "Evidence Reviewer",
    "evidence_reviewer",
    "write_case_patch",
    "invoice_calculation_valid",
)


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"Path escapes case directory: {relative}") from exc
    return candidate


def validate_dataset(root: Path) -> dict[str, Any]:
    root = root.resolve()
    dataset = _read_json(root / "dataset.json")
    case_entries = dataset.get("cases")
    if not isinstance(case_entries, list) or not case_entries:
        raise ValueError("dataset.json must contain a non-empty cases list")

    declared_count = int(dataset.get("case_count", -1))
    if declared_count != len(case_entries):
        raise ValueError(
            f"case_count mismatch: declared={declared_count}, actual={len(case_entries)}"
        )

    seen_case_ids: set[str] = set()
    pdf_hashes: set[str] = set()
    split_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    source_ids_by_split: dict[str, set[str]] = {}

    for entry in case_entries:
        if not isinstance(entry, dict):
            raise ValueError("Every dataset case entry must be an object")
        case_id = str(entry.get("case_id", "")).strip()
        relative_dir = str(entry.get("path", "")).strip()
        if not case_id or case_id in seen_case_ids:
            raise ValueError(f"Duplicate or empty case_id: {case_id!r}")
        seen_case_ids.add(case_id)
        case_dir = _inside(root, relative_dir)
        manifest = _read_json(case_dir / "manifest.json")
        scenario = _read_json(case_dir / "scenario.json")
        expected = _read_json(case_dir / "expected.json")
        oracle_text_path = case_dir / "oracle" / "source_text.md"
        oracle_truth_path = case_dir / "oracle" / "source_ground_truth.json"
        if not oracle_text_path.is_file() or not oracle_truth_path.is_file():
            raise ValueError(f"Missing oracle material for {case_id}")
        oracle_text = oracle_text_path.read_text(encoding="utf-8")

        ids = {manifest.get("case_id"), scenario.get("id"), expected.get("case_id")}
        if ids != {case_id}:
            raise ValueError(f"Case ID mismatch in {case_dir}: {ids}")

        split = str(manifest.get("split", ""))
        if split not in {"dev", "validation", "holdout"}:
            raise ValueError(f"Unsupported split for {case_id}: {split}")
        split_counts[split] += 1

        messages = "\n".join(
            str(turn.get("message", ""))
            for turn in scenario.get("user_script", [])
            if isinstance(turn, dict)
        )
        for token in INTERNAL_PROMPT_TOKENS:
            if token.lower() in messages.lower():
                raise ValueError(f"Internal benchmark token leaked into {case_id}: {token}")
        sentinel = str(expected.get("oracle_sentinel", ""))
        if not sentinel or sentinel in json.dumps(scenario, ensure_ascii=False):
            raise ValueError(f"Oracle sentinel missing or leaked for {case_id}")

        sources = manifest.get("sources", [])
        attachments = manifest.get("attachments", [])
        if not isinstance(sources, list) or not sources:
            raise ValueError(f"No sources declared for {case_id}")
        if not isinstance(attachments, list) or not attachments:
            raise ValueError(f"No attachments declared for {case_id}")
        source_ids = {str(source.get("source_id", "")) for source in sources}
        if "" in source_ids or len(source_ids) != len(sources):
            raise ValueError(f"Invalid source IDs for {case_id}")

        for source in sources:
            license_info = source.get("license") or {}
            if not license_info.get("spdx") or not license_info.get("url"):
                raise ValueError(f"Missing source license for {case_id}")
            if license_info.get("redistribution_allowed") is not True:
                raise ValueError(f"Source is not redistributable for {case_id}")
            canonical_url = str(source.get("canonical_url", ""))
            commit = str(source.get("repository_commit", ""))
            if not canonical_url or not commit:
                raise ValueError(f"Unpinned source for {case_id}")
            if "/main/" in canonical_url or "/master/" in canonical_url:
                raise ValueError(f"Moving source URL for {case_id}: {canonical_url}")
            source_key = f"{source.get('sha256', '')}:{commit}"
            previous_splits = source_ids_by_split.setdefault(source_key, set())
            previous_splits.add(split)
            if len(previous_splits) > 1:
                raise ValueError(f"Source lineage crosses splits: {source_key}")

        attachment_ids: set[str] = set()
        for attachment in attachments:
            attachment_id = str(attachment.get("id", ""))
            if not attachment_id or attachment_id in attachment_ids:
                raise ValueError(f"Invalid attachment ID in {case_id}: {attachment_id!r}")
            attachment_ids.add(attachment_id)
            if attachment.get("source_id") not in source_ids:
                raise ValueError(f"Unknown attachment source in {case_id}: {attachment_id}")
            attachment_path = _inside(case_dir, str(attachment.get("path", "")))
            if not attachment_path.is_file():
                raise ValueError(f"Missing attachment in {case_id}: {attachment_path}")
            if attachment_path.stat().st_size != int(attachment.get("byte_size", -1)):
                raise ValueError(f"Attachment size mismatch in {case_id}: {attachment_id}")
            actual_hash = _sha256(attachment_path)
            if actual_hash != attachment.get("sha256"):
                raise ValueError(f"Attachment hash mismatch in {case_id}: {attachment_id}")
            if attachment.get("media_type") == "application/pdf":
                if not attachment_path.read_bytes().startswith(b"%PDF-"):
                    raise ValueError(f"Invalid PDF signature in {case_id}: {attachment_id}")
                pdf_hashes.add(actual_hash)

        scenario_paths = {
            str(path)
            for turn in scenario.get("user_script", [])
            if isinstance(turn, dict)
            for path in turn.get("attach", [])
        }
        declared_paths = {str(item.get("path", "")) for item in attachments}
        if scenario_paths != declared_paths:
            raise ValueError(
                f"Scenario/manifest attachment mismatch in {case_id}: "
                f"scenario={scenario_paths}, manifest={declared_paths}"
            )

        proofs = expected.get("proofs") or {}
        if not isinstance(proofs, dict) or not proofs:
            raise ValueError(f"No proof expectations for {case_id}")
        for requirement_id, proof in proofs.items():
            status = str((proof or {}).get("status", ""))
            if status not in TRISTATES:
                raise ValueError(
                    f"Invalid proof status in {case_id}/{requirement_id}: {status}"
                )
            status_counts[status] += 1
            if status == "NOT_FOUND":
                forbidden = set(expected.get("forbidden_strong_conclusions", {}).get(requirement_id, []))
                if not {"SUPPORTED", "CONTRADICTED"}.issubset(forbidden):
                    raise ValueError(
                        f"NOT_FOUND must forbid both strong states in {case_id}/{requirement_id}"
                    )

        for fact in expected.get("oracle_facts", []):
            evidence = fact.get("evidence", []) if isinstance(fact, dict) else []
            for citation in evidence:
                if citation.get("attachment_id") not in attachment_ids:
                    raise ValueError(f"Unknown oracle attachment in {case_id}: {citation}")
                if not citation.get("locator") or not citation.get("quote"):
                    raise ValueError(f"Ungrounded oracle fact in {case_id}: {fact.get('id')}")
                if str(citation["quote"]) not in oracle_text:
                    raise ValueError(
                        f"Oracle quote is not exact source text in {case_id}: {fact.get('id')}"
                    )

    expected_splits = dataset.get("split_counts") or {}
    if dict(split_counts) != expected_splits:
        raise ValueError(
            f"split_counts mismatch: declared={expected_splits}, actual={dict(split_counts)}"
        )
    declared_unique = int(dataset.get("unique_pdf_count", -1))
    if declared_unique != len(pdf_hashes):
        raise ValueError(
            f"unique_pdf_count mismatch: declared={declared_unique}, actual={len(pdf_hashes)}"
        )

    return {
        "dataset_id": dataset.get("dataset_id"),
        "case_count": len(case_entries),
        "unique_pdf_count": len(pdf_hashes),
        "split_counts": dict(split_counts),
        "proof_status_counts": dict(status_counts),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate an InvoiceTauBench dataset bundle")
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    print(json.dumps(validate_dataset(args.root), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
