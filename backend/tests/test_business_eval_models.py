from __future__ import annotations

import hashlib
import json
import shutil
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.evals.business.models import (
    FrameworkOracle,
    RequiredToolOracle,
    load_case,
    load_oracle,
    validate_case_bundle,
    validate_case_input,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
REFERENCE_CASE = (
    REPO_ROOT
    / "evals"
    / "business_v1"
    / "cases"
    / "invoice_arithmetic_conflict_001"
)
ALL_CASES_ROOT = REPO_ROOT / "evals" / "business_v1" / "cases"


def _copy_case(tmp_path: Path) -> Path:
    destination = tmp_path / REFERENCE_CASE.name
    shutil.copytree(REFERENCE_CASE, destination)
    return destination


def _update_json(path: Path, update: Callable[[dict[str, Any]], None]) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    update(payload)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def test_reference_case_and_oracle_load_with_verified_attachment_hash() -> None:
    loaded_case = load_case(REFERENCE_CASE)
    loaded_oracle = load_oracle(REFERENCE_CASE)
    case, oracle = validate_case_bundle(REFERENCE_CASE)

    assert loaded_case == case
    assert loaded_oracle == oracle
    assert case.case_id == oracle.case_id == "invoice_arithmetic_conflict_001"
    assert oracle.schema_version == "2"
    assert len(oracle.facts) == 27
    assert {milestone.id for milestone in oracle.milestones} == {
        "line_extensions",
        "subtotal_aggregation",
        "stated_component_rate_base_validation",
        "final_total_reconciliation",
    }
    assert len(oracle.relations) == 11
    assert len(oracle.epistemic_boundaries) == 3

    attachment = case.attachments[0]
    attachment_path = REFERENCE_CASE / attachment.path
    assert hashlib.sha256(attachment_path.read_bytes()).hexdigest() == attachment.sha256


def test_business_invoice_oracles_have_strict_three_way_distribution() -> None:
    case_dirs = sorted(path for path in ALL_CASES_ROOT.iterdir() if path.is_dir())
    bundles = [validate_case_bundle(path) for path in case_dirs]

    assert len(bundles) == 10
    assert Counter(oracle.requirement.decision_status for _, oracle in bundles) == {
        "SUPPORTED": 3,
        "CONTRADICTED": 5,
        "NOT_FOUND": 2,
    }
    for _, oracle in bundles:
        assert all(fact.predicate_options for fact in oracle.facts if fact.origin == "source")
        assert all(milestone.facet_ref for milestone in oracle.milestones)
        assert oracle.communication.opposite_conclusions
        if oracle.requirement.decision_status == "NOT_FOUND":
            assert oracle.requirement.projected_status == "weak"
            assert oracle.requirement.blocking_obligations is True
            assert any(milestone.missing_meaning for milestone in oracle.milestones)
        else:
            assert not any(milestone.missing_meaning for milestone in oracle.milestones)


@pytest.mark.parametrize(
    "case_id",
    ["invoice_subtotal_conflict_0006", "mixed_vat_subtotal_conflict_0044"],
)
def test_subtotal_conflicts_preserve_both_downstream_total_paths(case_id: str) -> None:
    oracle = load_oracle(ALL_CASES_ROOT / case_id)
    milestones = {milestone.id: milestone for milestone in oracle.milestones}

    assert milestones["subtotal_aggregation"].expected_status == "CONTRADICTED"
    assert milestones["printed_subtotal_total_reconciliation"].expected_status == "CONTRADICTED"
    assert milestones["line_derived_total_reconciliation"].expected_status == "SUPPORTED"
    assert milestones["printed_subtotal_total_reconciliation"].facet_ref == "final_total"
    assert milestones["line_derived_total_reconciliation"].facet_ref == "final_total"


@pytest.mark.parametrize(
    ("case_id", "expected_component_status"),
    [
        ("invoice_arithmetic_conflict_001", "NOT_FOUND"),
        ("invoice_arithmetic_supported_0005", "SUPPORTED"),
        ("invoice_subtotal_conflict_0006", "NOT_FOUND"),
        ("credit_note_arithmetic_supported_0014", "SUPPORTED"),
        ("credit_note_total_conflict_0016", "SUPPORTED"),
        ("invoice_total_conflict_0025", "SUPPORTED"),
        ("mixed_vat_subtotal_conflict_0044", "NOT_FOUND"),
        ("tax_inclusive_arithmetic_supported_0053", "NOT_FOUND"),
        ("mixed_vat_arithmetic_supported_0062", "NOT_FOUND"),
    ],
)
def test_stated_component_rate_base_boundary_is_explicit(
    case_id: str,
    expected_component_status: str,
) -> None:
    oracle = load_oracle(ALL_CASES_ROOT / case_id)
    milestone = next(
        item for item in oracle.milestones if item.id == "stated_component_rate_base_validation"
    )

    assert milestone.expected_status == expected_component_status


def test_case_and_oracle_ids_must_match(tmp_path: Path) -> None:
    case_dir = _copy_case(tmp_path)
    _update_json(
        case_dir / "oracle.json",
        lambda payload: payload.update(case_id="another_case"),
    )

    with pytest.raises(ValueError, match="case_id must match"):
        validate_case_bundle(case_dir)


def test_runtime_input_validation_does_not_require_or_load_oracle(tmp_path: Path) -> None:
    case_dir = _copy_case(tmp_path)
    (case_dir / "oracle.json").unlink()

    case = validate_case_input(case_dir)

    assert case.case_id == "invoice_arithmetic_conflict_001"


def test_attachment_cannot_escape_case_directory(tmp_path: Path) -> None:
    case_dir = _copy_case(tmp_path)
    outside = tmp_path / "outside.pdf"
    outside.write_bytes((case_dir / "attachments" / "invoice.pdf").read_bytes())
    outside_hash = hashlib.sha256(outside.read_bytes()).hexdigest()

    def point_outside(payload: dict[str, Any]) -> None:
        attachment = payload["attachments"][0]
        attachment["path"] = "../outside.pdf"
        attachment["sha256"] = outside_hash

    _update_json(case_dir / "case.json", point_outside)

    with pytest.raises(ValueError, match="escapes case directory or is missing"):
        validate_case_bundle(case_dir)


def test_attachment_must_exist(tmp_path: Path) -> None:
    case_dir = _copy_case(tmp_path)

    def point_to_missing(payload: dict[str, Any]) -> None:
        payload["attachments"][0]["path"] = "attachments/missing.pdf"

    _update_json(case_dir / "case.json", point_to_missing)

    with pytest.raises(ValueError, match="escapes case directory or is missing"):
        validate_case_bundle(case_dir)


def test_attachment_hash_tampering_is_rejected(tmp_path: Path) -> None:
    case_dir = _copy_case(tmp_path)
    attachment = case_dir / "attachments" / "invoice.pdf"
    attachment.write_bytes(attachment.read_bytes() + b"tampered")

    with pytest.raises(ValueError, match="attachment hash mismatch"):
        validate_case_bundle(case_dir)


@pytest.mark.parametrize("filename", ["case.json", "oracle.json"])
def test_unknown_case_bundle_fields_are_rejected(tmp_path: Path, filename: str) -> None:
    case_dir = _copy_case(tmp_path)
    _update_json(
        case_dir / filename,
        lambda payload: payload.update(unexpected_field="must fail closed"),
    )

    loader = load_case if filename == "case.json" else load_oracle
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        loader(case_dir)


def test_oracle_sentinel_must_not_appear_in_case_json(tmp_path: Path) -> None:
    case_dir = _copy_case(tmp_path)
    sentinel = load_oracle(case_dir).sentinel
    _update_json(
        case_dir / "case.json",
        lambda payload: payload.update(title=f"leaked: {sentinel}"),
    )

    with pytest.raises(ValueError, match="oracle sentinel leaked"):
        validate_case_bundle(case_dir)


def test_oracle_rejects_dangling_milestone_fact_reference(tmp_path: Path) -> None:
    case_dir = _copy_case(tmp_path)

    def add_dangling_ref(payload: dict[str, Any]) -> None:
        payload["milestones"][0]["fact_ids"].append("fact_does_not_exist")

    _update_json(case_dir / "oracle.json", add_dangling_ref)

    with pytest.raises(ValidationError, match="dangling refs"):
        load_oracle(case_dir)


def test_oracle_rejects_incorrect_arithmetic_relation(tmp_path: Path) -> None:
    case_dir = _copy_case(tmp_path)

    def corrupt_recomputed_total(payload: dict[str, Any]) -> None:
        fact = next(item for item in payload["facts"] if item["id"] == "recomputed_total")
        fact["value"] = "183313.84"

    _update_json(case_dir / "oracle.json", corrupt_recomputed_total)

    with pytest.raises(ValidationError, match="recomputed_total_math.*not self-consistent"):
        load_oracle(case_dir)


def test_oracle_fact_origin_bindings_fail_closed(tmp_path: Path) -> None:
    case_dir = _copy_case(tmp_path)

    def give_derived_fact_a_source(payload: dict[str, Any]) -> None:
        fact = next(item for item in payload["facts"] if item["id"] == "total_difference")
        fact["source_role"] = "invoice"

    _update_json(case_dir / "oracle.json", give_derived_fact_a_source)

    with pytest.raises(ValidationError, match="derived facts cannot declare"):
        load_oracle(case_dir)


def test_framework_oracle_rejects_required_forbidden_overlap() -> None:
    with pytest.raises(ValidationError, match="both required and forbidden"):
        FrameworkOracle(
            required_tools=[RequiredToolOracle(name="functions.read_attachment")],
            forbidden_tools=["read_attachment"],
        )


def test_framework_oracle_rejects_orphan_or_forbidden_approval() -> None:
    with pytest.raises(ValidationError, match="approved tools cannot be forbidden"):
        FrameworkOracle(
            required_approved_tools=["write_case_file"],
            forbidden_tools=["write_case_file"],
        )
    with pytest.raises(ValidationError, match="must correspond"):
        FrameworkOracle(required_approved_tools=["render_pdf"])


def test_framework_oracle_rejects_impossible_total_call_budget() -> None:
    with pytest.raises(ValidationError, match="lower than required minimum"):
        FrameworkOracle(
            required_tools=[
                RequiredToolOracle(name="read_source", min_calls=2),
                RequiredToolOracle(name="submit_check", min_calls=1),
            ],
            max_total_calls=2,
        )
