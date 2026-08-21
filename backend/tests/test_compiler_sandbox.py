from __future__ import annotations

from decimal import Decimal

import pytest

from app.compiler_runtime.models import Claim, EvidenceIR
from app.compiler_runtime.proof_terms import ProofTermRef
from app.compiler_runtime.sandbox import EvidenceSandbox, SourceRecord


def _sandbox(*, ir: EvidenceIR | None = None) -> EvidenceSandbox:
    return EvidenceSandbox(
        sources=[
            SourceRecord(
                source_id="invoice-1",
                title="Invoice 1",
                kind="invoice",
                content="Invoice INV-42\nSupplier: Acme Ltd\nTotal GBP 10,500\nStatus: open",
            )
        ],
        allowed_check_ids=["check-total", "check-supplier"],
        allowed_check_facets={
            "check-total": ["subtotal_aggregation", "final_total"],
            "check-supplier": ["supplier_identity"],
        },
        allowed_check_policy_refs={
            "check-total": ["invoice_calculation_rounding_tolerance"],
        },
        policy_values={
            "invoice_calculation_rounding_tolerance": {
                "value": "0.01",
                "unit": "document_currency",
            }
        },
        policy_snapshot_hash="policy-v1",
        evidence_ir=ir,
    )


def _bind_total(sandbox: EvidenceSandbox, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "subject": "invoice:INV-42",
        "predicate": "invoice.total",
        "value": {"amount": "10500", "currency": "GBP"},
        "source_id": "invoice-1",
        "quote": "Total GBP 10,500",
        "locator": "line 3",
        "confidence": "high",
        "attributes": {"basis": "gross", "currency": "GBP"},
    }
    payload.update(overrides)
    return sandbox.bind_claim(**payload)


def test_only_listed_sources_can_be_read_and_listing_hides_content() -> None:
    sandbox = _sandbox()

    listed = sandbox.list_sources()

    assert listed == {
        "ok": True,
        "action": "list_sources",
        "sources": [
            {
                "source_id": "invoice-1",
                "title": "Invoice 1",
                "kind": "invoice",
                "characters": 63,
            }
        ],
    }
    assert "content" not in listed["sources"][0]

    missing = sandbox.read_source("not-in-run")
    assert missing["ok"] is False
    assert missing["error"]["code"] == "SOURCE_NOT_FOUND"
    assert missing["error"]["repair"]
    assert sandbox.read_source_ids == ()

    read = sandbox.read_source("invoice-1")
    assert read["ok"] is True
    assert read["source"]["content"].startswith("Invoice INV-42")
    assert sandbox.read_source_ids == ("invoice-1",)


def test_claim_requires_a_prior_read_and_exact_source_quote() -> None:
    sandbox = _sandbox()

    unread = _bind_total(sandbox)
    assert unread["error"]["code"] == "SOURCE_NOT_READ"
    assert sandbox.evidence_ir.claims == []

    sandbox.read_source("invoice-1")
    wrong_quote = _bind_total(sandbox, quote="Total GBP 10,501")
    assert wrong_quote["error"]["code"] == "QUOTE_NOT_IN_SOURCE"
    assert wrong_quote["error"]["repair"]
    assert sandbox.evidence_ir.claims == []


def test_system_provenance_is_read_gated_and_quoteable_without_claiming_authenticity() -> None:
    sandbox = EvidenceSandbox(
        sources=[
            SourceRecord(
                source_id="invoice-upload",
                content="Invoice INV-42",
                provenance={
                    "runtime_admission": "admitted",
                    "attachment_id": "att-42",
                    "original_ref": "attachments/originals/invoice.pdf",
                    "content_sha256": "abc123",
                    "scope": "system_chain_of_custody_only_not_real_world_authenticity",
                },
            )
        ],
        allowed_check_ids=["check-traceability"],
    )

    assert "system_provenance" not in sandbox.list_sources()["sources"][0]
    read = sandbox.read_source("invoice-upload")
    assert read["source"]["system_provenance"]["attachment_id"] == "att-42"

    bound = sandbox.bind_claim(
        subject="invoice-upload",
        predicate="system_provenance_traceable",
        value=True,
        source_id="invoice-upload",
        quote='"original_ref": "attachments/originals/invoice.pdf"',
        locator="system_provenance.original_ref",
        confidence="high",
    )

    assert bound["ok"] is True
    assert bound["claim"]["quote"] == '"original_ref": "attachments/originals/invoice.pdf"'
    assert "authentic" not in bound["claim"]["predicate"]


@pytest.mark.parametrize(
    ("locator", "error_code"),
    [
        ("", "LOCATOR_INVALID"),
        (0, "LOCATOR_INVALID"),
        ("line 99", "LOCATOR_OUT_OF_RANGE"),
        ("lines 3-2", "LOCATOR_OUT_OF_RANGE"),
    ],
)
def test_invalid_locator_is_repairable_and_never_enters_ir(
    locator: str | int,
    error_code: str,
) -> None:
    sandbox = _sandbox()
    sandbox.read_source("invoice-1")

    result = _bind_total(sandbox, locator=locator)

    assert result["ok"] is False
    assert result["error"]["code"] == error_code
    assert result["error"]["repair"]
    assert sandbox.evidence_ir.claims == []


def test_claim_admission_is_append_only_and_semantically_idempotent() -> None:
    sandbox = _sandbox()
    sandbox.read_source("invoice-1")

    created = _bind_total(sandbox, locator=3)
    duplicate = _bind_total(sandbox, locator="3", claim_id="model-invented-id")

    assert created["ok"] is True
    assert created["created"] is True
    assert created["claim"]["locator"] == "line 3"
    assert created["claim"]["attributes"] == {"basis": "gross", "currency": "GBP"}
    assert duplicate["ok"] is True
    assert duplicate["created"] is False
    assert duplicate["duplicate"] is True
    assert duplicate["claim"]["id"] == created["claim"]["id"]
    assert len(sandbox.evidence_ir.claims) == 1


def test_focused_write_scope_rejects_non_focused_check_without_mutation() -> None:
    sandbox = _sandbox()
    sandbox.read_source("invoice-1")
    claim_id = _bind_total(sandbox)["claim"]["id"]
    before_claims = sandbox.evidence_ir.claims
    before_submissions = sandbox.submissions
    before_bindings = sandbox.binding_proposals
    before_witnesses = sandbox.calculation_witnesses

    with sandbox.focused_writes(["check-total"]):
        submitted = sandbox.submit_check(
            check_id="check-supplier",
            claim_ids=[claim_id],
            note="hostile cross-check write",
        )
        computed = sandbox.compute_witness(
            check_id="check-supplier",
            facet_ref="supplier_identity",
            operation="SUM",
            refs=[{"kind": "CLAIM", "ref_id": claim_id}],
        )

    assert submitted["ok"] is False
    assert submitted["error"]["code"] == "CHECK_OUTSIDE_FOCUS"
    assert submitted["error"]["details"]["focused_check_ids"] == ["check-total"]
    assert computed["ok"] is False
    assert computed["error"]["code"] == "CHECK_OUTSIDE_FOCUS"
    assert sandbox.evidence_ir.claims == before_claims
    assert sandbox.submissions == before_submissions
    assert sandbox.binding_proposals == before_bindings
    assert sandbox.calculation_witnesses == before_witnesses

    detached = sandbox.evidence_ir
    detached.claims.clear()
    assert len(sandbox.evidence_ir.claims) == 1


def test_conflicting_claim_id_and_schema_error_do_not_mutate_ir() -> None:
    sandbox = _sandbox()
    sandbox.read_source("invoice-1")
    first = _bind_total(sandbox, claim_id="claim-fixed")
    assert first["ok"] is True

    conflict = sandbox.bind_claim(
        claim_id="claim-fixed",
        subject="invoice:INV-42",
        predicate="invoice.status",
        value="open",
        source_id="invoice-1",
        quote="Status: open",
        locator="line 4",
    )
    invalid_confidence = sandbox.bind_claim(
        subject="invoice:INV-42",
        predicate="invoice.status",
        value="open",
        source_id="invoice-1",
        quote="Status: open",
        locator="line 4",
        confidence="certain",
    )

    assert conflict["error"]["code"] == "CLAIM_ID_CONFLICT"
    assert invalid_confidence["error"]["code"] == "CLAIM_SHAPE_INVALID"
    assert len(sandbox.evidence_ir.claims) == 1


def test_submit_check_accepts_only_plan_checks_and_admitted_claims() -> None:
    sandbox = _sandbox()

    wrong_check = sandbox.submit_check(check_id="invented", claim_ids=[])
    missing_claim = sandbox.submit_check(check_id="check-total", claim_ids=["claim-missing"])
    assert wrong_check["error"]["code"] == "CHECK_NOT_IN_PLAN"
    assert missing_claim["error"]["code"] == "CLAIM_REFERENCE_NOT_FOUND"
    assert sandbox.submissions == ()

    sandbox.read_source("invoice-1")
    claim_id = _bind_total(sandbox)["claim"]["id"]
    created = sandbox.submit_check(
        check_id="check-total",
        claim_ids=[claim_id, claim_id],
        note="Ready for independent verification",
    )
    duplicate = sandbox.submit_check(
        check_id="check-total",
        claim_ids=[claim_id],
        note="Ready for independent verification",
        submission_id="ignored-for-semantic-duplicate",
    )

    assert created["created"] is True
    assert created["submission"]["claim_ids"] == [claim_id]
    assert duplicate["created"] is False
    assert duplicate["submission"]["submission_id"] == created["submission"]["submission_id"]
    assert len(sandbox.submissions) == 1


def test_seeded_ir_is_preserved_and_public_capabilities_stay_narrow() -> None:
    seeded = Claim(
        id="claim-seeded",
        subject="vendor:ACME",
        predicate="vendor.status",
        value="active",
        source_id="historical-source",
        quote="ACME is active",
        locator="line 1",
        confidence="high",
    )
    sandbox = _sandbox(
        ir=EvidenceIR(schema_version="seed-v1", source_ids=["historical-source"], claims=[seeded])
    )

    assert sandbox.evidence_ir.schema_version == "seed-v1"
    assert sandbox.evidence_ir.source_ids == ["historical-source", "invoice-1"]
    assert [claim.id for claim in sandbox.evidence_ir.claims] == ["claim-seeded"]
    assert sandbox.capability_names == (
        "list_sources",
        "read_source",
        "bind_claim",
        "compute_witness",
        "submit_check",
    )
    assert not hasattr(sandbox, "run_shell")
    assert not hasattr(sandbox, "run_python")
    assert not hasattr(sandbox, "write_case")


def test_claim_cannot_smuggle_semantic_relation_into_observation_attributes() -> None:
    sandbox = _sandbox()
    sandbox.read_source("invoice-1")

    result = _bind_total(
        sandbox,
        attributes={
            "currency": "GBP",
            "relation": "serves_as_discount_base_for",
            "related_claim_ids": ["claim-rate"],
        },
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "CLAIM_SHAPE_INVALID"
    assert sandbox.evidence_ir.claims == []


def test_compute_witness_reconciles_localized_quote_and_document_currency_policy() -> None:
    source = SourceRecord(
        source_id="invoice-eur",
        content="Printed total 1.234,56.\nExpected total 1.234,55.",
        kind="invoice",
    )
    sandbox = EvidenceSandbox(
        sources=[source],
        allowed_check_ids=["check-total"],
        allowed_check_facets={"check-total": ["final_total"]},
        allowed_check_policy_refs={
            "check-total": ["invoice_calculation_rounding_tolerance"]
        },
        policy_values={
            "invoice_calculation_rounding_tolerance": {
                "value": "0.01",
                "unit": "document_currency",
            }
        },
        policy_snapshot_hash="policy-v1",
    )
    sandbox.read_source("invoice-eur")
    printed = sandbox.bind_claim(
        subject="invoice:1",
        predicate="printed_total",
        value="1234.56",
        source_id="invoice-eur",
        quote="Printed total 1.234,56.",
        locator="line 1",
        attributes={"currency": "EUR"},
    )["claim"]["id"]
    expected = sandbox.bind_claim(
        subject="invoice:1",
        predicate="expected_total",
        value="1234.55",
        source_id="invoice-eur",
        quote="Expected total 1.234,55.",
        locator="line 2",
        attributes={"currency": "EUR"},
    )["claim"]["id"]

    difference = sandbox.compute_witness(
        check_id="check-total",
        facet_ref="final_total",
        operation="ABS_DIFF",
        refs=[
            ProofTermRef(kind="CLAIM", ref_id=printed),
            ProofTermRef(kind="CLAIM", ref_id=expected),
        ],
    )
    assert difference["ok"] is True
    assert difference["witness"]["result"] == "0.01"
    comparison = sandbox.compute_witness(
        check_id="check-total",
        facet_ref="final_total",
        operation="GREATER_THAN",
        refs=[
            ProofTermRef(kind="WITNESS", ref_id=difference["witness"]["id"]),
            ProofTermRef(
                kind="POLICY",
                ref_id="invoice_calculation_rounding_tolerance",
            ),
        ],
    )

    assert comparison["ok"] is True
    assert comparison["witness"]["result"] is False
    policy_operand = comparison["witness"]["operands"][1]
    assert policy_operand["currency"] == "EUR"
    assert policy_operand["unit"] == ""
    assert sandbox.resolved_policy_terms["invoice_calculation_rounding_tolerance"] == {
        "value": "0.01",
        "currency": "EUR",
        "unit": "",
    }


@pytest.mark.parametrize(
    ("quote", "value", "accepted"),
    [
        ("Total 1234.56.", "1234.56", True),
        ("Credit (1.234,56).", "-1234.56", True),
        ("Discount 20%.", "0.20", True),
        ("Discount 20%.", "20", False),
        ("Discount 20%.", "20%", False),
        ("1 11'303.20 11'303.20", "1", True),
        ("1 11'303.20 11'303.20", "11303.20", True),
        ("Total 11 303,20.", "11303.20", True),
        ("Total 57.708,12 EUR.", "57708.12", True),
        ("Grouped 1,234,567 units.", "1234567", True),
        ("Grouped 1.234.567 units.", "1234567", True),
        ("Ambiguous 1,234.", "1234", False),
        ("Ambiguous 1,234.", "1.234", False),
        ("Malformed 1,2,3.", "123", False),
        ("Malformed 1,2,3.", "12.3", False),
        ("Malformed 1.234.56.", "1234.56", False),
        ("Malformed 1.234.56.", "12.3", False),
        ("1 11'303.20 11'303.20", "11304.20", False),
        ("Total 1.234,56.", "999", False),
        ("Total 11'303.20.", "11303.20", True),
        ("Total 11'303.20.", Decimal("11303.20"), True),
        ("Total 11'303.20.", 11303.2, False),
    ],
)
def test_numeric_claims_fail_fast_against_localized_source_observation(
    quote: str,
    value: object,
    accepted: bool,
) -> None:
    sandbox = EvidenceSandbox(
        sources=[SourceRecord(source_id="numeric-source", content=quote)],
        allowed_check_ids=["check-total"],
        allowed_check_facets={"check-total": ["final_total"]},
    )
    sandbox.read_source("numeric-source")
    bound = sandbox.bind_claim(
        subject="invoice:1",
        predicate="observed_numeric_value",
        value=value,
        source_id="numeric-source",
        quote=quote,
        locator="line 1",
    )

    assert bound["ok"] is accepted
    if not accepted:
        assert bound["error"]["code"] == "CLAIM_VALUE_NOT_OBSERVED"
        if "%" in quote:
            assert bound["error"]["repair"] == (
                "Re-bind the Claim with a canonical Decimal factor string matching the printed "
                "percentage (for example, printed 20% -> Claim value string '0.20'; JSON float "
                "0.2, value 20, and string '20%' are invalid for that example)."
            )
        else:
            assert bound["error"]["repair"] == (
                "Re-bind the Claim with a canonical Decimal string for the localized number "
                "actually printed in its quote; do not use a JSON float."
            )
        assert sandbox.evidence_ir.claims == []
        return
    if isinstance(value, str):
        assert bound["claim"]["value"] == value

    result = sandbox.compute_witness(
        check_id="check-total",
        facet_ref="final_total",
        operation="SUM",
        refs=[ProofTermRef(kind="CLAIM", ref_id=bound["claim"]["id"])],
    )

    assert result["ok"] is True


@pytest.mark.parametrize(
    ("quote", "rejected_values", "expected_repair"),
    [
        (
            "Observed rate 20%.",
            (0.2, 20, "20%"),
            "Re-bind the Claim with a canonical Decimal factor string matching the printed "
            "percentage (for example, printed 20% -> Claim value string '0.20'; JSON float "
            "0.2, value 20, and string '20%' are invalid for that example).",
        ),
        (
            "Observed amount 11'303.20.",
            (11303.2, "999"),
            "Re-bind the Claim with a canonical Decimal string for the localized number "
            "actually printed in its quote; do not use a JSON float.",
        ),
    ],
)
def test_numeric_repair_is_stable_and_rejections_leave_ir_unchanged(
    quote: str,
    rejected_values: tuple[object, ...],
    expected_repair: str,
) -> None:
    sandbox = EvidenceSandbox(
        sources=[SourceRecord(source_id="numeric-source", content=quote)],
        allowed_check_ids=["check-value"],
    )
    sandbox.read_source("numeric-source")
    original_ir = sandbox.evidence_ir.model_dump(mode="json")

    for value in rejected_values:
        result = sandbox.bind_claim(
            claim_id="claim-retry",
            subject="record:1",
            predicate="observed_numeric_value",
            value=value,
            source_id="numeric-source",
            quote=quote,
            locator="line 1",
        )

        assert result["error"]["code"] == "CLAIM_VALUE_NOT_OBSERVED"
        assert result["error"]["repair"] == expected_repair
        assert sandbox.evidence_ir.model_dump(mode="json") == original_ir


def test_rejected_numeric_claim_does_not_reserve_its_requested_id() -> None:
    sandbox = EvidenceSandbox(
        sources=[SourceRecord(source_id="numeric-source", content="Total 11'303.20.")],
        allowed_check_ids=["check-total"],
    )
    sandbox.read_source("numeric-source")
    common = {
        "claim_id": "claim-fixed",
        "subject": "invoice:1",
        "predicate": "printed_total",
        "source_id": "numeric-source",
        "quote": "Total 11'303.20.",
        "locator": "line 1",
    }

    rejected = sandbox.bind_claim(value=11303.2, **common)
    accepted = sandbox.bind_claim(value="11303.20", **common)

    assert rejected["error"]["code"] == "CLAIM_VALUE_NOT_OBSERVED"
    assert accepted["ok"] is True
    assert accepted["claim"]["id"] == "claim-fixed"
    assert [claim.id for claim in sandbox.evidence_ir.claims] == ["claim-fixed"]


def test_bind_claim_numeric_gate_does_not_reject_text_observations() -> None:
    sandbox = EvidenceSandbox(
        sources=[SourceRecord(source_id="status-source", content="Status open; reference 1234")],
        allowed_check_ids=["check-status"],
    )
    sandbox.read_source("status-source")

    result = sandbox.bind_claim(
        subject="invoice:1",
        predicate="status",
        value="open",
        source_id="status-source",
        quote="Status open; reference 1234",
        locator="line 1",
    )

    assert result["ok"] is True
    assert result["claim"]["value"] == "open"


def test_compute_witness_defensively_rechecks_seeded_numeric_claim() -> None:
    invalid = Claim(
        id="claim-invalid-numeric",
        subject="invoice:1",
        predicate="printed_total",
        value="999",
        source_id="numeric-source",
        quote="Total 1.234,56.",
        locator="line 1",
    )
    sandbox = EvidenceSandbox(
        sources=[SourceRecord(source_id="numeric-source", content="Total 1.234,56.")],
        allowed_check_ids=["check-total"],
        allowed_check_facets={"check-total": ["final_total"]},
        evidence_ir=EvidenceIR(source_ids=["numeric-source"], claims=[invalid]),
    )

    result = sandbox.compute_witness(
        check_id="check-total",
        facet_ref="final_total",
        operation="SUM",
        refs=[ProofTermRef(kind="CLAIM", ref_id=invalid.id)],
    )

    assert result["error"]["code"] == "CLAIM_VALUE_NOT_OBSERVED"
    assert result["error"]["repair"] == (
        "Re-bind the Claim with a canonical Decimal string for the localized number "
        "actually printed in its quote; do not use a JSON float."
    )


def test_claims_remain_append_only_after_witness_and_cross_check_submission_is_rejected() -> None:
    sandbox = _sandbox()
    sandbox.read_source("invoice-1")
    claim_id = _bind_total(
        sandbox,
        value="42",
        quote="Invoice INV-42",
        locator="line 1",
        attributes={},
    )["claim"]["id"]
    witness = sandbox.compute_witness(
        check_id="check-total",
        facet_ref="final_total",
        operation="SUM",
        refs=[ProofTermRef(kind="CLAIM", ref_id=claim_id)],
    )
    assert witness["ok"] is True

    late_claim = sandbox.bind_claim(
        subject="invoice:INV-42",
        predicate="status",
        value="open",
        source_id="invoice-1",
        quote="Status: open",
        locator="line 4",
    )
    assert late_claim["ok"] is True
    assert late_claim["created"] is True
    assert witness["witness"]["evidence_snapshot_hash"] == (
        sandbox.evidence_ir.source_snapshot_hash()
    )

    cross_check = sandbox.submit_check(
        check_id="check-supplier",
        witness_ids=[witness["witness"]["id"]],
    )
    assert cross_check["error"]["code"] == "WITNESS_CHECK_MISMATCH"


def test_binding_proposal_must_belong_to_current_check_and_submitted_terms() -> None:
    sandbox = _sandbox()
    sandbox.read_source("invoice-1")
    claim_id = _bind_total(sandbox)["claim"]["id"]

    result = sandbox.submit_check(
        check_id="check-total",
        claim_ids=[claim_id],
        binding_proposals=[
            {
                "id": "binding-cross-check",
                "check_id": "check-supplier",
                "facet_ref": "supplier_identity",
                "relation": "serves_as_total_for",
                "term_refs": [{"kind": "CLAIM", "ref_id": claim_id}],
                "reason": "proposal only",
            }
        ],
    )

    assert result["error"]["code"] == "BINDING_SCOPE_MISMATCH"
    assert sandbox.binding_proposals == ()
