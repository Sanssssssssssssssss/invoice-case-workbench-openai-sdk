from __future__ import annotations

from decimal import Decimal

import pytest

from app.compiler_runtime.consumer import (
    CanonicalConsumerPacket,
    ConsumerBinding,
    ConsumerClaim,
    ConsumerLeafFinding,
    ConsumerLineage,
    ConsumerRootDecision,
    finalize_consumer_report,
    validate_canonical_report_projection,
)
from app.compiler_runtime.proof_terms import (
    CalculationWitness,
    ProofTermRef,
    ResolvedProofTerm,
)


def _packet() -> CanonicalConsumerPacket:
    claims = [
        ConsumerClaim(
            id="claim_total",
            subject="invoice:INV-25",
            predicate="stated_total",
            value="13156.92",
            source_id="source_invoice",
            quote="Total EUR 13,156.92; unrelated source text EUR 999999.99",
            locator="invoice.pdf:page=1",
            confidence="high",
            currency="EUR",
        ),
        ConsumerClaim(
            id="claim_adjustment",
            subject="invoice:INV-25",
            predicate="adjustment",
            value="-406.92",
            source_id="source_invoice",
            quote="Adjustment (406.92)",
            locator="invoice.pdf:page=1",
            confidence="high",
            currency="EUR",
        ),
        ConsumerClaim(
            id="claim_rate",
            subject="invoice:INV-25",
            predicate="tax_rate",
            value="0.20",
            source_id="source_invoice",
            quote="VAT 20%",
            locator="invoice.pdf:page=1",
            confidence="high",
        ),
    ]
    witness = CalculationWitness(
        id="witness_policy_rate",
        check_id="check_total",
        facet_ref="final_total",
        operation="SUM",
        operands=[
            ResolvedProofTerm(
                ref=ProofTermRef(kind="POLICY", ref_id="rounding_tolerance"),
                value=Decimal("0.01"),
                currency="EUR",
            )
        ],
        result=Decimal("0.01"),
        currency="EUR",
        evidence_snapshot_hash="evidence-hash",
        policy_snapshot_hash="policy-hash",
        lineage_hash="witness-lineage",
    )
    return CanonicalConsumerPacket(
        case_id="case-report-projection",
        lineage=ConsumerLineage(),
        execution_status="COMPLETED",
        review_complete=True,
        decision_ready=True,
        reportability="FULL",
        root_decisions=[
            ConsumerRootDecision(
                requirement_id="invoice_calculation_valid",
                required=True,
                root_node_id="check_total",
                status="CONTRADICTED",
                contradicting_check_ids=["check_total"],
                leaf_finding_ids=["invoice_calculation_valid::check_total"],
                decision_ready=True,
            )
        ],
        leaf_findings=[
            ConsumerLeafFinding(
                id="invoice_calculation_valid::check_total",
                root_requirement_id="invoice_calculation_valid",
                check_id="check_total",
                facet_refs=["final_total"],
                status="CONTRADICTED",
                claim_ids=[item.id for item in claims],
                binding_ids=["binding_total"],
                witness_ids=[witness.id],
                source_ids=["source_invoice"],
                decisive_for_root=True,
            )
        ],
        claims=claims,
        binding_proposals=[
            ConsumerBinding(
                id="binding_total",
                check_id="check_total",
                facet_ref="final_total",
                relation="stated total role",
                term_refs=[ProofTermRef(kind="CLAIM", ref_id="claim_total")],
            )
        ],
        calculation_witnesses=[witness],
        source_fingerprints={"source_invoice": "sha256:source"},
    )


def test_rejects_hostile_amount_even_when_it_appears_only_in_claim_quote() -> None:
    with pytest.raises(ValueError, match="business numeric value.*999999.99"):
        finalize_consumer_report("# 报告\n\n总额：EUR 999999.99", _packet())


@pytest.mark.parametrize(
    "rendered",
    [
        "EUR 13,156.92",
        "EUR 13.156,92",
        "EUR 13'156.92",
        "EUR 13’156.92",
        "EUR (406.92)",
        "税率 20%",
        "VAT 20%",
        "容差 EUR 0,01",
    ],
)
def test_accepts_packet_values_in_english_german_swiss_and_percent_forms(
    rendered: str,
) -> None:
    assert validate_canonical_report_projection(rendered, _packet()) == rendered


@pytest.mark.parametrize(
    "rendered",
    [
        "发票总额13,156.92 EUR",
        "- 13,156.92 EUR",
        "  - 13,156.92 EUR",
        "- -406.92 EUR",
    ],
)
def test_accepts_cjk_adjacent_amounts_and_does_not_treat_list_markers_as_signs(
    rendered: str,
) -> None:
    assert validate_canonical_report_projection(rendered, _packet()) == rendered


def test_markdown_list_marker_does_not_admit_an_invented_amount() -> None:
    with pytest.raises(ValueError, match="business numeric value.*999999.99"):
        validate_canonical_report_projection("- 999999.99 EUR", _packet())


def test_rejects_unadmitted_percentage() -> None:
    with pytest.raises(ValueError, match="business numeric value.*21%"):
        validate_canonical_report_projection("税率：21%", _packet())


def test_factor_percentage_does_not_accept_same_display_number() -> None:
    with pytest.raises(ValueError, match="business numeric value.*0.20%"):
        validate_canonical_report_projection("税率：0.20%", _packet())


def test_percent_unit_still_uses_canonical_factor_convention() -> None:
    packet = _packet()
    packet.claims = [
        item.model_copy(update={"unit": "%"}) if item.id == "claim_rate" else item
        for item in packet.claims
    ]

    assert validate_canonical_report_projection("税率：20%", packet) == "税率：20%"
    with pytest.raises(ValueError, match="business numeric value.*0.20%"):
        validate_canonical_report_projection("税率：0.20%", packet)


def test_percent_literal_in_claim_value_is_normalized_to_factor() -> None:
    packet = _packet()
    packet.claims = [
        item.model_copy(update={"value": "20%"}) if item.id == "claim_rate" else item
        for item in packet.claims
    ]

    assert validate_canonical_report_projection("税率：20%", packet) == "税率：20%"


def test_percent_literal_inside_canonical_claim_text_is_admitted() -> None:
    packet = _packet()
    packet.claims = [
        item.model_copy(
            update={
                "value": "Statutory VAT (20%) applies",
                "quote": "Statutory VAT (20%) applies; unrelated 21%",
            }
        )
        if item.id == "claim_rate"
        else item
        for item in packet.claims
    ]

    assert validate_canonical_report_projection("法定增值税率：20%", packet) == "法定增值税率：20%"
    with pytest.raises(ValueError, match="business numeric value.*21%"):
        validate_canonical_report_projection("税率：21%", packet)


@pytest.mark.parametrize(
    "invented_currency",
    [
        "USD 13,156.92",
        "CNY 13,156.92",
        "KWD 13,156.92",
        "$13,156.92",
        "CHF 13'156.92",
    ],
)
def test_rejects_packet_amount_relabelled_as_another_currency(
    invented_currency: str,
) -> None:
    with pytest.raises(ValueError, match="business numeric value"):
        validate_canonical_report_projection(invented_currency, _packet())


@pytest.mark.parametrize(
    "invented",
    [
        "Claim ID: claim_fabricated",
        "Binding ID：binding_fabricated",
        "Witness ID: witness_fabricated",
        "Check ID: check_fabricated",
        "Requirement ID: fabricated_scope",
        "证明编号：fabricated_proof",
    ],
)
def test_rejects_explicit_proof_ids_outside_packet(invented: str) -> None:
    with pytest.raises(ValueError, match="proof id outside canonical consumer packet"):
        validate_canonical_report_projection(invented, _packet())


def test_accepts_only_gap_codes_projected_by_the_packet() -> None:
    packet = _packet().model_copy(
        update={
            "leaf_findings": [
                _packet().leaf_findings[0].model_copy(
                    update={"gap_code": "WITNESS_MISSING"}
                )
            ]
        }
    )

    assert validate_canonical_report_projection("缺口：WITNESS_MISSING", packet)
    with pytest.raises(ValueError, match="proof id outside canonical consumer packet"):
        validate_canonical_report_projection("缺口：BINDING_MISSING", packet)


def test_accepts_current_underscore_ids_and_validates_markdown_table_columns() -> None:
    markdown = """\
| 要求编号 | 叶检查 | 三态结论 | 主张/绑定/计算编号 |
|---|---|---|---|
| invoice_calculation_valid | check_total | CONTRADICTED | claim_total / binding_total / witness_policy_rate |
"""

    assert validate_canonical_report_projection(markdown, _packet()) == markdown


def test_rejects_plain_invented_requirement_in_explicit_table_column() -> None:
    markdown = """\
| 要求编号 | 三态结论 |
|---|---|
| fabricated_scope | CONTRADICTED |
"""

    with pytest.raises(ValueError, match="proof id outside canonical consumer packet"):
        validate_canonical_report_projection(markdown, _packet())


@pytest.mark.parametrize(
    "markdown",
    [
        "要求编号：invoice_calculation_valid；状态：SUPPORTED",
        "reportability: PARTIAL",
        "execution_status: FAILED",
        "review_complete: false",
        "decision_ready: false",
        "integrity_rejected: true",
    ],
)
def test_rejects_explicit_statuses_that_conflict_with_packet(markdown: str) -> None:
    with pytest.raises(ValueError, match="status conflicts with canonical consumer packet"):
        validate_canonical_report_projection(markdown, _packet())


def test_rejects_leaf_status_mismatch_in_markdown_table() -> None:
    markdown = """\
| 要求编号 | 叶检查 | 三态结论 |
|---|---|---|
| invoice_calculation_valid | check_total | SUPPORTED |
"""

    with pytest.raises(ValueError, match="check_total=SUPPORTED"):
        validate_canonical_report_projection(markdown, _packet())


def test_ignores_dates_pages_sections_versions_and_source_document_ids() -> None:
    markdown = """\
# 审查报告

- 日期：2026-08-22
- 归档月份：2026.08
- 页码：3.00
- 版本：2.10
- 章节：4.20
- 发票编号：INV-999999.99
- 订单编号：ORDER-888888.88
- 来源发票编号：CLAIM-2026-001

## 2.10 证明结果
"""

    assert validate_canonical_report_projection(markdown, _packet()) == markdown
