from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.compiler_runtime.models import Claim
from app.compiler_runtime.proof_terms import (
    CalculationRequest,
    ProofTermRef,
    ProofTermResolutionError,
    ProofTermTypeError,
    ResolvedProofTerm,
    SemanticBindingProposal,
    claim_content_hash,
    compute_witness,
    replay_witness,
)


EVIDENCE_HASH = "sha256:evidence-v1"
POLICY_HASH = "sha256:policy-v1"
CHECK_ID = "check.invoice_arithmetic"
FACET_REF = "invoice_calculation.final_total"


def _claim(
    claim_id: str,
    value: str,
    *,
    currency: str = "USD",
    unit: str = "",
    quote: str | None = None,
) -> Claim:
    return Claim(
        id=claim_id,
        subject="invoice:INV-1",
        predicate=claim_id.removeprefix("claim."),
        value=value,
        source_id="source.invoice",
        quote=quote or value,
        locator="page 1",
        confidence="high",
        attributes={"currency": currency, "unit": unit},
    )


def _ref(kind: str, ref_id: str) -> ProofTermRef:
    return ProofTermRef(kind=kind, ref_id=ref_id)


def _compute(request: CalculationRequest, *, claims: dict[str, Claim], witnesses=None, policies=None):
    return compute_witness(
        request,
        claims=claims,
        witnesses=witnesses or {},
        policy_values=policies or {},
        evidence_snapshot_hash=EVIDENCE_HASH,
        policy_snapshot_hash=POLICY_HASH,
    )


def test_model_request_can_only_supply_operation_and_references() -> None:
    with pytest.raises(ValidationError, match="check_id"):
        CalculationRequest(
            id="w.unbound",
            facet_ref=FACET_REF,
            operation="SUM",
            operands=[_ref("CLAIM", "claim.a")],
        )

    with pytest.raises(ValidationError, match="result"):
        CalculationRequest(
            id="w.sum",
            check_id=CHECK_ID,
            facet_ref=FACET_REF,
            operation="SUM",
            operands=[_ref("CLAIM", "claim.a")],
            result="999",
        )

    with pytest.raises(ValidationError, match="tolerance"):
        CalculationRequest(
            id="w.compare",
            check_id=CHECK_ID,
            facet_ref=FACET_REF,
            operation="GREATER_THAN",
            operands=[_ref("CLAIM", "claim.a"), _ref("POLICY", "policy.tolerance")],
            tolerance="0.01",
        )


def test_resolved_operand_lineage_fields_are_kind_exclusive() -> None:
    with pytest.raises(ValidationError, match="CLAIM operands require claim_content_hash"):
        ResolvedProofTerm(ref=_ref("CLAIM", "claim.a"), value="1")
    with pytest.raises(ValidationError, match="only CLAIM operands may have claim_content_hash"):
        ResolvedProofTerm(
            ref=_ref("POLICY", "policy.a"),
            value="1",
            claim_content_hash="sha256:not-allowed",
        )


def test_semantic_binding_is_only_a_proposal_and_has_no_verdict() -> None:
    proposal = SemanticBindingProposal(
        id="binding.discount_base",
        check_id=CHECK_ID,
        facet_ref="invoice_calculation.stated_components",
        relation="serves_as_discount_base_for",
        term_refs=[_ref("CLAIM", "claim.subtotal"), _ref("CLAIM", "claim.discount_rate")],
        reason="The invoice layout associates the displayed subtotal and adjustment rate.",
    )
    assert proposal.relation == "serves_as_discount_base_for"

    with pytest.raises(ValidationError, match="verdict"):
        SemanticBindingProposal(
            id="binding.discount_base",
            check_id=CHECK_ID,
            facet_ref="invoice_calculation.stated_components",
            relation="serves_as_discount_base_for",
            term_refs=[_ref("CLAIM", "claim.subtotal")],
            reason="Model assertion",
            verdict="ACCEPTED",
        )


def test_compute_uses_canonical_claim_value_not_localized_quote() -> None:
    claim = _claim(
        "claim.total",
        "1234.56",
        currency="EUR",
        quote="Gesamt 1.234,56 EUR",
    )
    witness = _compute(
        CalculationRequest(
            id="w.total",
            check_id=CHECK_ID,
            facet_ref=FACET_REF,
            operation="SUM",
            operands=[_ref("CLAIM", claim.id)],
        ),
        claims={claim.id: claim},
    )

    assert witness.result == Decimal("1234.56")
    assert witness.operands[0].value == Decimal("1234.56")
    assert witness.operands[0].claim_content_hash == claim_content_hash(claim)
    assert "1.234,56" not in witness.model_dump_json()
    assert replay_witness(witness, claims={claim.id: claim}, witnesses={}, policy_values={})


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("id", "claim.changed"),
        ("source_id", "source.changed"),
        ("quote", "Changed quote 1234.56"),
        ("locator", "page 9"),
        ("subject", "invoice:OTHER"),
        ("predicate", "changed_predicate"),
        ("value", "1234.57"),
        ("confidence", "low"),
        ("attributes", {"currency": "EUR", "unit": "changed"}),
    ],
)
def test_claim_operand_commits_every_claim_field(field: str, replacement: object) -> None:
    claim = _claim("claim.total", "1234.56", currency="EUR")
    witness = _compute(
        CalculationRequest(
            id="w.total",
            check_id=CHECK_ID,
            facet_ref=FACET_REF,
            operation="SUM",
            operands=[_ref("CLAIM", claim.id)],
        ),
        claims={claim.id: claim},
    )
    changed = claim.model_copy(update={field: replacement}, deep=True)

    assert not replay_witness(
        witness,
        claims={claim.id: changed},
        witnesses={},
        policy_values={},
    )


def test_unrelated_late_claim_does_not_change_claim_operand_commitment() -> None:
    claim = _claim("claim.total", "1234.56", currency="EUR")
    witness = _compute(
        CalculationRequest(
            id="w.total",
            check_id=CHECK_ID,
            facet_ref=FACET_REF,
            operation="SUM",
            operands=[_ref("CLAIM", claim.id)],
        ),
        claims={claim.id: claim},
    )
    late = _claim("claim.late", "7", currency="EUR")

    assert replay_witness(
        witness,
        claims={claim.id: claim, late.id: late},
        witnesses={},
        policy_values={},
    )


def test_unknown_reference_fails_closed() -> None:
    with pytest.raises(ProofTermResolutionError, match="unknown CLAIM ref"):
        _compute(
            CalculationRequest(
                id="w.missing",
                check_id=CHECK_ID,
                facet_ref=FACET_REF,
                operation="SUM",
                operands=[_ref("CLAIM", "claim.missing")],
            ),
            claims={},
        )


def test_currency_conflict_is_not_silently_calculated() -> None:
    usd = _claim("claim.usd", "10", currency="USD")
    eur = _claim("claim.eur", "5", currency="EUR")

    with pytest.raises(ProofTermTypeError, match="currency mismatch"):
        _compute(
            CalculationRequest(
                id="w.invalid_sum",
                check_id=CHECK_ID,
                facet_ref=FACET_REF,
                operation="SUM",
                operands=[_ref("CLAIM", usd.id), _ref("CLAIM", eur.id)],
            ),
            claims={usd.id: usd, eur.id: eur},
        )


def test_multiply_allows_one_count_unit_and_one_currency_operand() -> None:
    quantity = _claim("claim.quantity", "2", currency="", unit="each")
    unit_price = _claim("claim.unit_price", "10", currency="EUR")

    witness = _compute(
        CalculationRequest(
            id="w.line_extension",
            check_id=CHECK_ID,
            facet_ref="invoice_calculation.line_extensions",
            operation="MULTIPLY",
            operands=[_ref("CLAIM", quantity.id), _ref("CLAIM", unit_price.id)],
        ),
        claims={quantity.id: quantity, unit_price.id: unit_price},
    )

    assert witness.result == Decimal("20")
    assert witness.currency == "EUR"
    assert witness.unit == ""
    assert replay_witness(
        witness,
        claims={quantity.id: quantity, unit_price.id: unit_price},
        witnesses={},
        policy_values={},
    )


def test_multiply_allows_dimensionless_rate_but_rejects_ambiguous_dimensions() -> None:
    rate = _claim("claim.rate", "0.20", currency="")
    base = _claim("claim.base", "100", currency="EUR")
    witness = _compute(
        CalculationRequest(
            id="w.component",
            check_id=CHECK_ID,
            facet_ref="invoice_calculation.stated_components",
            operation="MULTIPLY",
            operands=[_ref("CLAIM", rate.id), _ref("CLAIM", base.id)],
        ),
        claims={rate.id: rate, base.id: base},
    )
    assert witness.result == Decimal("20.00")
    assert witness.currency == "EUR"

    usd = _claim("claim.usd", "10", currency="USD")
    with pytest.raises(ProofTermTypeError, match="currency mismatch"):
        _compute(
            CalculationRequest(
                id="w.cross_currency",
                check_id=CHECK_ID,
                facet_ref=FACET_REF,
                operation="MULTIPLY",
                operands=[_ref("CLAIM", base.id), _ref("CLAIM", usd.id)],
            ),
            claims={base.id: base, usd.id: usd},
        )

    each = _claim("claim.each", "2", currency="", unit="each")
    boxes = _claim("claim.boxes", "3", currency="", unit="box")
    with pytest.raises(ProofTermTypeError, match="count-unit"):
        _compute(
            CalculationRequest(
                id="w.ambiguous_units",
                check_id=CHECK_ID,
                facet_ref=FACET_REF,
                operation="MULTIPLY",
                operands=[_ref("CLAIM", each.id), _ref("CLAIM", boxes.id)],
            ),
            claims={each.id: each, boxes.id: boxes},
        )


def test_witness_chain_replays_line_sum_total_difference_and_policy_comparison() -> None:
    claims = {
        "claim.line_1": _claim("claim.line_1", "10.00"),
        "claim.line_2": _claim("claim.line_2", "5.00"),
        "claim.tax": _claim("claim.tax", "2.00"),
        "claim.printed_total": _claim("claim.printed_total", "16.50"),
    }
    witnesses = {}

    line_sum = _compute(
        CalculationRequest(
            id="w.line_sum",
            check_id=CHECK_ID,
            facet_ref="invoice_calculation.line_extensions",
            operation="SUM",
            operands=[_ref("CLAIM", "claim.line_1"), _ref("CLAIM", "claim.line_2")],
        ),
        claims=claims,
    )
    witnesses[line_sum.id] = line_sum

    recomputed_total = _compute(
        CalculationRequest(
            id="w.recomputed_total",
            check_id=CHECK_ID,
            facet_ref=FACET_REF,
            operation="SUM",
            operands=[_ref("WITNESS", line_sum.id), _ref("CLAIM", "claim.tax")],
        ),
        claims=claims,
        witnesses=witnesses,
    )
    witnesses[recomputed_total.id] = recomputed_total

    difference = _compute(
        CalculationRequest(
            id="w.difference",
            check_id=CHECK_ID,
            facet_ref=FACET_REF,
            operation="ABS_DIFF",
            operands=[
                _ref("WITNESS", recomputed_total.id),
                _ref("CLAIM", "claim.printed_total"),
            ],
        ),
        claims=claims,
        witnesses=witnesses,
    )
    witnesses[difference.id] = difference

    comparison = _compute(
        CalculationRequest(
            id="w.above_tolerance",
            check_id=CHECK_ID,
            facet_ref=FACET_REF,
            operation="GREATER_THAN",
            operands=[_ref("WITNESS", difference.id), _ref("POLICY", "policy.tolerance")],
        ),
        claims=claims,
        witnesses=witnesses,
        policies={"policy.tolerance": "0.01"},
    )

    assert line_sum.result == Decimal("15.00")
    assert recomputed_total.result == Decimal("17.00")
    assert difference.result == Decimal("0.50")
    assert comparison.result is True
    assert recomputed_total.operands[0].parent_lineage_hash == line_sum.lineage_hash
    assert difference.operands[0].parent_lineage_hash == recomputed_total.lineage_hash
    assert comparison.operands[0].parent_lineage_hash == difference.lineage_hash
    assert replay_witness(
        comparison,
        claims=claims,
        witnesses=witnesses,
        policy_values={"policy.tolerance": "0.01"},
    )


def test_tampered_witness_or_parent_chain_does_not_replay() -> None:
    claims = {
        "claim.a": _claim("claim.a", "10"),
        "claim.b": _claim("claim.b", "5"),
    }
    parent = _compute(
        CalculationRequest(
            id="w.parent",
            check_id=CHECK_ID,
            facet_ref=FACET_REF,
            operation="SUM",
            operands=[_ref("CLAIM", "claim.a"), _ref("CLAIM", "claim.b")],
        ),
        claims=claims,
    )
    child = _compute(
        CalculationRequest(
            id="w.child",
            check_id=CHECK_ID,
            facet_ref=FACET_REF,
            operation="SUBTRACT",
            operands=[_ref("WITNESS", parent.id), _ref("CLAIM", "claim.b")],
        ),
        claims=claims,
        witnesses={parent.id: parent},
    )

    tampered_child = child.model_copy(update={"result": Decimal("999")})
    assert not replay_witness(
        tampered_child,
        claims=claims,
        witnesses={parent.id: parent},
        policy_values={},
    )

    tampered_check = child.model_copy(update={"check_id": "check.unrelated"})
    assert not replay_witness(
        tampered_check,
        claims=claims,
        witnesses={parent.id: parent},
        policy_values={},
    )

    tampered_facet = child.model_copy(update={"facet_ref": "invoice_calculation.unrelated"})
    assert not replay_witness(
        tampered_facet,
        claims=claims,
        witnesses={parent.id: parent},
        policy_values={},
    )

    tampered_parent = parent.model_copy(update={"result": Decimal("999")})
    assert not replay_witness(
        child,
        claims=claims,
        witnesses={parent.id: tampered_parent},
        policy_values={},
    )


def test_policy_value_is_runtime_resolved_and_dimension_checked() -> None:
    difference = _claim("claim.difference", "0.50", currency="USD")
    request = CalculationRequest(
        id="w.compare",
        check_id=CHECK_ID,
        facet_ref=FACET_REF,
        operation="GREATER_THAN",
        operands=[_ref("CLAIM", difference.id), _ref("POLICY", "policy.tolerance")],
    )
    witness = _compute(
        request,
        claims={difference.id: difference},
        policies={"policy.tolerance": {"value": "0.01", "currency": "USD"}},
    )
    assert witness.result is True

    with pytest.raises(ProofTermTypeError, match="policy dimension mismatch"):
        _compute(
            request.model_copy(update={"id": "w.compare_wrong_currency"}),
            claims={difference.id: difference},
            policies={"policy.tolerance": {"value": "0.01", "currency": "EUR"}},
        )


def test_snapshot_hash_prevents_cross_snapshot_witness_reuse() -> None:
    claim = _claim("claim.a", "10")
    parent = _compute(
        CalculationRequest(
            id="w.parent",
            check_id=CHECK_ID,
            facet_ref=FACET_REF,
            operation="SUM",
            operands=[_ref("CLAIM", claim.id)],
        ),
        claims={claim.id: claim},
    )

    with pytest.raises(ProofTermResolutionError, match="different evidence snapshot"):
        compute_witness(
            CalculationRequest(
                id="w.child",
                check_id=CHECK_ID,
                facet_ref=FACET_REF,
                operation="SUM",
                operands=[_ref("WITNESS", parent.id)],
            ),
            claims={claim.id: claim},
            witnesses={parent.id: parent},
            policy_values={},
            evidence_snapshot_hash="sha256:evidence-v2",
            policy_snapshot_hash=POLICY_HASH,
        )
