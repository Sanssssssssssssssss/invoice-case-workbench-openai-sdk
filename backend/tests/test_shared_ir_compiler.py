from __future__ import annotations

from copy import deepcopy
import hashlib

import pytest

from app.domain.invoice_contracts import build_requirement_contracts, contract_hole_id
from app.domain.invoice_proof_compiler import compile_evidence_proof
from app.domain.invoice_requirements import KNOWN_REQUIREMENTS, REQUIREMENT_DEFINITIONS, REQUIREMENT_PACK
from app.state.schemas import CompiledProof, EvidenceItem, SemanticClaimCandidate, SemanticProposalCandidate


def _claim_candidate(contract, row: dict[str, object]) -> SemanticClaimCandidate:
    input_ = next(
        item
        for item in contract.inputs
        if item.hole_kind in {"claim", "relation"}
        and item.subject == row["subject"]
        and item.predicate == row["predicate"]
    )
    attributes = {
        key: row[key]
        for key in ("unit", "currency", "basis", "tax_basis", "coverage")
        if row.get(key) not in {None, ""}
    }
    quote = str(row["source_quote"])
    locator = str(row["source_locator"])
    return SemanticClaimCandidate.model_validate({
        "handle": str(row["id"]),
        "hole_id": contract_hole_id(input_, contract),
        "typed_value": row["typed_value"],
        "source_quote": quote,
        "source_locator": locator,
        "confidence": "high",
        "entity_handle": str(row.get("entity_key") or "") if input_.binding_mode in {"same_entity", "per_entity"} else "",
        "attributes": attributes,
        "attribute_sources": {
            key: {"source_quote": quote, "source_locator": locator}
            for key in attributes
        } if row.get("_ground_attributes", True) else {},
    })


def _proposal_candidate(
    contract,
    *,
    verdict: str,
    reason: str,
    input_handles: list[str],
    entity_handle: str = "",
    stale: bool = False,
) -> SemanticProposalCandidate:
    judgment = next(item for item in contract.inputs if item.hole_kind == "judgment")
    return SemanticProposalCandidate.model_validate({
        "handle": f"proposal:{contract.requirement_id}",
        "hole_id": "hole:stale" if stale else contract_hole_id(judgment, contract),
        "verdict": verdict,
        "input_handles": input_handles,
        "supporting_handles": input_handles if verdict == "SUPPORTED" else [],
        "opposing_handles": input_handles if verdict == "REFUTED" else [],
        "entity_handle": entity_handle,
        "open_questions": ["semantic interpretation remains unresolved"] if verdict == "UNKNOWN" else [],
        "confidence": "high",
        "reason": reason,
    })


def _vendor_packet(*, stale_hash: bool = False, locator: str = "vendor.md line 2"):
    contracts, _ = build_requirement_contracts({"vendor_identity", "vendor_identity_active"})
    contract = next(item for item in contracts if item.requirement_id == "vendor_identity_active")
    quote = "Vendor V-42 status active effective 2026-01-01"
    claims = [
        {
            "id": "CLM_VENDOR_IDENTITY",
            "subject": "vendor",
            "predicate": "identity",
            "entity_key": "V-42",
            "value_type": "string",
            "typed_value": "V-42",
            "source_quote": quote,
            "source_locator": "vendor.md line 1",
            "confidence": "high",
        },
        {
            "id": "CLM_VENDOR_STATUS",
            "subject": "vendor",
            "predicate": "status",
            "entity_key": "V-42",
            "value_type": "enum",
            "typed_value": "active",
            "source_quote": quote,
            "source_locator": locator,
            "confidence": "high",
        },
        {
            "id": "CLM_VENDOR_STATUS_EFFECTIVE_DATE",
            "subject": "vendor",
            "predicate": "status_effective_date",
            "entity_key": "V-42",
            "value_type": "date",
            "typed_value": "2026-01-01",
            "source_quote": quote,
            "source_locator": "vendor.md line 3",
            "confidence": "high",
        },
    ]
    refs = [
        {
            "claim_id": claim["id"],
            "evidence_id": "ev_vendor",
            "source_quote": quote,
            "source_locator": claim["source_locator"],
        }
        for claim in claims
    ]
    target = next(item.predicate for item in contract.inputs if item.hole_kind == "judgment")
    evidence = EvidenceItem(
        id="ev_vendor",
        type="vendor_record",
        source="attachment",
        credibility="high",
        content=quote,
        review_result={"should_accept": True},
        supports=[{"requirement": "vendor_identity", "support_level": "full", "quoted_text": quote}],
        metadata={"classification": "business_evidence"},
        semantic_claims=[_claim_candidate(contract, claim) for claim in claims],
        semantic_proposals=[_proposal_candidate(
            contract,
            verdict="SUPPORTED",
            reason="The current vendor status is active.",
            input_handles=[str(item["id"]) for item in claims],
            stale=stale_hash,
        )],
    )
    trusted = {
        evidence.id: {
            "texts": [quote],
            "sha256": hashlib.sha256(quote.encode()).hexdigest(),
            "original_ref": "vendor.md",
        }
    }
    return evidence, trusted


def _proposal_packet(
    *,
    active_requirement_ids: set[str],
    requirement_id: str,
    source_requirement: str,
    evidence_type: str,
    evidence_id: str,
    claim_id: str,
    subject: str,
    predicate: str,
    typed_value: str,
    quote: str,
):
    contracts, _ = build_requirement_contracts(active_requirement_ids)
    contract = next(item for item in contracts if item.requirement_id == requirement_id)
    locator = f"{evidence_id} line 1"
    claim = {
        "id": claim_id,
        "subject": subject,
        "predicate": predicate,
        "value_type": "string",
        "typed_value": typed_value,
        "source_quote": quote,
        "source_locator": locator,
        "confidence": "high",
    }
    ref = {
        "claim_id": claim_id,
        "evidence_id": evidence_id,
        "source_quote": quote,
        "source_locator": locator,
    }
    evidence = EvidenceItem(
        id=evidence_id,
        type=evidence_type,
        source="attachment",
        credibility="high",
        content=quote,
        review_result={"should_accept": True},
        supports=[{"requirement": source_requirement, "support_level": "full", "quoted_text": quote}],
        metadata={"classification": "business_evidence"},
        semantic_claims=[_claim_candidate(contract, claim)],
        semantic_proposals=[_proposal_candidate(
            contract,
            verdict="SUPPORTED",
            reason="The source-grounded semantic gate is supported.",
            input_handles=[claim_id],
        )],
    )
    trusted = {
        evidence.id: {
            "texts": [quote],
            "sha256": hashlib.sha256(quote.encode()).hexdigest(),
            "original_ref": f"{evidence_id}.md",
        }
    }
    return evidence, trusted


def _configured_contract_proof(
    requirement_id: str,
    *,
    verdict: str = "SUPPORTED",
    configure_policy: bool = True,
    numeric_mismatch: bool = False,
    partial_coverage: bool = False,
    ground_attributes: bool = True,
    quote_attributes: bool = True,
    invalid_amount_basis: bool = False,
    relation_mismatch: bool = False,
    candidate_only: bool = False,
    semantic_enum_phrase: bool = False,
    extra_active: set[str] | None = None,
):
    pack = deepcopy(REQUIREMENT_PACK)
    if configure_policy:
        for key in REQUIREMENT_DEFINITIONS[requirement_id].get("required_policy_values") or []:
            if key in pack["unconfigured_policy_values"]:
                pack[key] = "2"
                pack["unconfigured_policy_values"].remove(key)
    contract_config = pack["proof_contracts"].get(requirement_id) or {}
    active = {
        requirement_id,
        *REQUIREMENT_DEFINITIONS[requirement_id].get("premise_requirements", []),
        *(
            group[0]
            for group in contract_config.get("activation_requirement_groups") or []
            if group
        ),
    }
    active.update(extra_active or ())
    contracts, _ = build_requirement_contracts(active, pack=pack)
    contract = next(item for item in contracts if item.requirement_id == requirement_id)
    typed_inputs = [item for item in contract.inputs if item.hole_kind in {"claim", "relation"}]
    relation_values = {
        item.predicate: f"{item.predicate.upper()}-100"
        for item in typed_inputs
        if item.hole_kind == "relation"
    }
    claims: list[dict[str, object]] = []
    claims_by_role: dict[str, list[dict[str, object]]] = {}
    refs: list[dict[str, str]] = []
    numeric_index = 0
    for index, item in enumerate(typed_inputs, start=1):
        if requirement_id == "no_active_duplicate" and verdict == "SUPPORTED" and item.binding_mode == "per_entity":
            continue
        if requirement_id == "no_active_duplicate" and item.subject == "reversal":
            continue
        if candidate_only and item.binding_mode == "per_entity" and item.subject != "duplicate_search":
            continue
        if item.hole_kind == "relation":
            value = f"{relation_values[item.predicate]}-{index}" if relation_mismatch else relation_values[item.predicate]
        elif item.value_type == "decimal":
            numeric_index += 1
            value = "105" if numeric_mismatch and numeric_index == 1 else "100"
        elif item.value_type == "integer":
            value = "10"
        elif item.value_type == "date":
            value = "2026-01-01"
        elif item.value_type == "currency":
            value = "GBP"
        elif item.value_type == "boolean":
            value = True
        elif item.allowed_values:
            value = (
                "no_candidate"
                if requirement_id == "no_active_duplicate" and item.predicate == "result" and verdict == "SUPPORTED"
                else "candidate_found"
                if requirement_id == "no_active_duplicate" and item.predicate == "result"
                else "active_settled"
                if semantic_enum_phrase and item.predicate == "economic_effect"
                else item.allowed_values[0]
            )
        elif item.value_type == "enum":
            value = "valid"
        else:
            value = f"VALUE-{index}"
        attributes: dict[str, str] = {}
        for attribute in item.required_attributes:
            amount_basis = {
                "invoice": "invoice_total",
                "purchase_order": "order_total",
                "goods_receipt": "received_value",
            }.get(item.subject, "contract_scope")
            if invalid_amount_basis and requirement_id == "three_way_amount_match":
                amount_basis = "contract_scope"
            attributes[attribute] = {
                "unit": "EA" if requirement_id == "three_way_quantity_match" else "money",
                "currency": "GBP",
                "basis": (
                    "quantity_scope"
                    if requirement_id == "three_way_quantity_match"
                    else amount_basis
                    if requirement_id == "three_way_amount_match"
                    else "contract_scope"
                ),
                "tax_basis": "gross",
                "coverage": "partial" if partial_coverage and numeric_index == 1 else "full",
            }[attribute]
        quote = " ".join([
            item.subject,
            item.predicate,
            str(value),
            *(f"{key} {value_}" for key, value_ in attributes.items() if quote_attributes),
        ])
        if semantic_enum_phrase and item.predicate == "economic_effect":
            quote = "The earlier payment remains applied and no reversal or refund was found."
        locator = f"packet line {index}"
        claim_id = f"CLM_GENERIC_{index}"
        claim = {
            "id": claim_id,
            "subject": item.subject,
            "predicate": item.predicate,
            **({"entity_key": "entity:1"} if item.binding_mode in {"same_entity", "per_entity"} else {}),
            "value_type": item.value_type,
            "typed_value": value,
            "source_quote": quote,
            "source_locator": locator,
            "confidence": "high",
            "_ground_attributes": ground_attributes,
            **attributes,
        }
        claims.append(claim)
        claims_by_role.setdefault(item.role, []).append(claim)
        evidence_id = f"ev_{item.role}"
        refs.append({
            "claim_id": claim_id,
            "evidence_id": evidence_id,
            "source_quote": quote,
            "source_locator": locator,
        })
    target = next(item.predicate for item in contract.inputs if item.hole_kind == "judgment")
    proposal = {
        "id": f"PRP_{requirement_id.upper()}",
        "contract_id": contract.contract_id,
        "contract_hash": contract.contract_hash,
        "target_predicate": target,
        "verdict": verdict,
        "input_refs": refs,
        "supporting_refs": refs if verdict == "SUPPORTED" else [],
        "opposing_refs": refs if verdict == "REFUTED" else [],
        "open_questions": ["semantic interpretation remains unresolved"] if verdict == "UNKNOWN" else [],
        "confidence": "high",
        "reason": "Source-grounded Contract interpretation.",
    }
    carrier_role = contract.evidence_roles[0]
    evidence_types = {
        "invoice": "invoice",
        "purchase_order": "purchase_order",
        "goods_receipt_or_service_acceptance": "goods_receipt",
        "vendor_identity": "vendor_record",
        "duplicate_payment_screen": "duplicate_payment_check",
    }
    evidence_items = []
    trusted = {}
    for role in contract.evidence_roles:
        role_claims = claims_by_role.get(role, [])
        quotes = [str(item["source_quote"]) for item in role_claims]
        content = "\n".join(quotes) or f"Source evidence for {role}"
        evidence = EvidenceItem(
            id=f"ev_{role}",
            type=evidence_types.get(role, "unknown"),
            source="attachment",
            credibility="high",
            content=content,
            review_result={"should_accept": True},
            supports=[{
                "requirement": role,
                "support_level": "full",
                "quoted_text": quotes[0] if quotes else content,
            }],
            metadata={"classification": "business_evidence"},
            semantic_claims=[_claim_candidate(contract, claim) for claim in role_claims],
            semantic_proposals=[
                _proposal_candidate(
                    contract,
                    verdict=verdict,
                    reason="Source-grounded Contract interpretation.",
                    input_handles=[str(item["id"]) for item in claims],
                    entity_handle="entity:1" if any(claim.get("entity_key") for claim in claims) else "",
                )
            ] if role == carrier_role else [],
        )
        evidence_items.append(evidence)
        trusted[evidence.id] = {
            "texts": quotes or [content],
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
            "original_ref": f"{role}.md",
        }
    return compile_evidence_proof(
        evidence_items,
        active_requirement_ids=active,
        trusted_sources=trusted,
        contracts=contracts,
        requirement_pack=pack,
    )


def _payment_hold_proof(*, branch: str, verdict: str, complete_group: bool = True):
    requirement_id = "no_unresolved_payment_hold"
    active = {requirement_id, "exception_hold_record"}
    contracts, _ = build_requirement_contracts(active)
    contract = next(item for item in contracts if item.requirement_id == requirement_id)
    values = [
        ("hold_search", "result", "enum", branch, None),
    ]
    if branch == "hold_found":
        values.extend([
            ("hold", "identity", "string", "HOLD-100", "hold-100"),
            ("hold", "status", "enum", "open", "hold-100"),
        ])
        if complete_group:
            values.append(("hold", "resolution_status", "enum", "unresolved", "hold-100"))
    claims = []
    refs = []
    for index, (subject, predicate, value_type, value, entity_key) in enumerate(values, start=1):
        quote = f"{subject} {predicate} {value}"
        locator = f"hold export row {index}"
        claim_id = f"CLM_HOLD_{index}"
        claims.append({
            "id": claim_id,
            "subject": subject,
            "predicate": predicate,
            "value_type": value_type,
            "typed_value": value,
            "entity_key": entity_key,
            "source_quote": quote,
            "source_locator": locator,
            "confidence": "high",
        })
        refs.append({
            "claim_id": claim_id,
            "evidence_id": "ev_hold",
            "source_quote": quote,
            "source_locator": locator,
        })
    target = next(item.predicate for item in contract.inputs if item.hole_kind == "judgment")
    proposal = {
        "id": "PRP_HOLD",
        "contract_id": contract.contract_id,
        "contract_hash": contract.contract_hash,
        "target_predicate": target,
        "entity_scope": {"hold": "hold-100"} if branch == "hold_found" else {},
        "verdict": verdict,
        "input_refs": refs,
        "supporting_refs": refs if verdict == "SUPPORTED" else [],
        "opposing_refs": refs if verdict == "REFUTED" else [],
        "open_questions": [],
        "confidence": "high",
        "reason": "The hold lifecycle was interpreted from the source rows.",
    }
    quotes = [str(item["source_quote"]) for item in claims]
    evidence = EvidenceItem(
        id="ev_hold",
        type="unknown",
        source="attachment",
        credibility="high",
        content="\n".join(quotes),
        review_result={"should_accept": True},
        supports=[{"requirement": "exception_hold_record", "support_level": "full", "quoted_text": quotes[0]}],
        metadata={"classification": "business_evidence"},
        semantic_claims=[_claim_candidate(contract, claim) for claim in claims],
        semantic_proposals=[_proposal_candidate(
            contract,
            verdict=verdict,
            reason="The hold lifecycle was interpreted from the source rows.",
            input_handles=[str(item["id"]) for item in claims],
            entity_handle="hold-100" if branch == "hold_found" else "",
        )],
    )
    trusted = {
        evidence.id: {
            "texts": quotes,
            "sha256": hashlib.sha256(evidence.content.encode()).hexdigest(),
            "original_ref": "hold-export.md",
        }
    }
    return compile_evidence_proof(
        [evidence],
        active_requirement_ids=active,
        trusted_sources=trusted,
        contracts=contracts,
    )


def test_all_active_requirements_receive_one_proof_view() -> None:
    proof = compile_evidence_proof([], active_requirement_ids=set(KNOWN_REQUIREMENTS), trusted_sources={})

    assert len(proof.decisions) == len(KNOWN_REQUIREMENTS)
    assert {item.requirement_id for item in proof.decisions} == set(KNOWN_REQUIREMENTS)
    assert all(item.proof_status == "INCOMPLETE" for item in proof.decisions)


@pytest.mark.parametrize(
    "requirement_id",
    [
        "invoice_fields_valid",
        "invoice_calculation_valid",
        "vendor_identity_active",
        "vendor_bank_account_authorized",
        "approval_authority_satisfied",
        "sod_control_satisfied",
        "payment_release_authorized",
        "tax_and_account_coding_valid",
        "audit_chain_complete",
    ],
)
@pytest.mark.parametrize(
    ("verdict", "expected"),
    [("SUPPORTED", "PROVED"), ("REFUTED", "DISPROVED"), ("UNKNOWN", "INCOMPLETE")],
)
def test_semantic_contracts_have_three_state_fixtures(
    requirement_id: str,
    verdict: str,
    expected: str,
) -> None:
    proof = _configured_contract_proof(requirement_id, verdict=verdict)

    assert proof.decision_for(requirement_id).proof_status == expected


@pytest.mark.parametrize("requirement_id", ["three_way_amount_match", "three_way_quantity_match", "non_po_contract_match"])
@pytest.mark.parametrize(
    ("variant", "expected"),
    [("proved", "PROVED"), ("mismatch", "DISPROVED"), ("partial", "INCOMPLETE")],
)
def test_generic_reconciliation_contracts_have_three_state_fixtures(
    requirement_id: str,
    variant: str,
    expected: str,
) -> None:
    proof = _configured_contract_proof(
        requirement_id,
        numeric_mismatch=variant == "mismatch",
        partial_coverage=variant == "partial",
    )

    assert proof.decision_for(requirement_id).proof_status == expected


def test_reconciliation_attributes_must_be_source_grounded() -> None:
    proof = _configured_contract_proof(
        "three_way_quantity_match",
        ground_attributes=False,
    )

    assert proof.decision_for("three_way_quantity_match").proof_status == "INCOMPLETE"
    assert any(
        item.code == "ATTRIBUTE_SOURCE_MISSING"
        for item in proof.diagnostics
    )


def test_reconciliation_attribute_value_must_appear_in_its_quote() -> None:
    proof = _configured_contract_proof(
        "three_way_amount_match",
        quote_attributes=False,
    )

    assert proof.decision_for("three_way_amount_match").proof_status == "INCOMPLETE"
    assert any(item.code == "ATTRIBUTE_VALUE_MISMATCH" for item in proof.diagnostics)


def test_reconciliation_executes_allowed_amount_basis_policy() -> None:
    proof = _configured_contract_proof(
        "three_way_amount_match",
        invalid_amount_basis=True,
    )

    assert proof.decision_for("three_way_amount_match").proof_status == "INCOMPLETE"
    assert any(
        item.operator == "allowed_by_subject" and item.status == "INCOMPLETE"
        for item in proof.checks
        if item.requirement_id == "three_way_amount_match"
    )


def test_reconciliation_relation_identity_is_interpreted_by_the_proposal() -> None:
    proof = _configured_contract_proof(
        "non_po_contract_match",
        relation_mismatch=True,
    )

    assert proof.decision_for("non_po_contract_match").proof_status == "PROVED"


@pytest.mark.parametrize(
    ("branch", "verdict", "complete_group", "expected"),
    [
        ("no_hold", "SUPPORTED", True, "PROVED"),
        ("hold_found", "REFUTED", True, "DISPROVED"),
        ("hold_found", "SUPPORTED", False, "INCOMPLETE"),
    ],
)
def test_payment_hold_lifecycle_has_three_state_fixtures(
    branch: str,
    verdict: str,
    complete_group: bool,
    expected: str,
) -> None:
    proof = _payment_hold_proof(
        branch=branch,
        verdict=verdict,
        complete_group=complete_group,
    )

    assert proof.decision_for("no_unresolved_payment_hold").proof_status == expected


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [("SUPPORTED", "PROVED"), ("REFUTED", "DISPROVED"), ("UNKNOWN", "INCOMPLETE")],
)
def test_duplicate_lifecycle_has_three_state_fixtures(verdict: str, expected: str) -> None:
    proof = _configured_contract_proof("no_active_duplicate", verdict=verdict)

    assert proof.decision_for("no_active_duplicate").proof_status == expected


def test_duplicate_candidate_without_payment_lifecycle_cannot_disprove() -> None:
    proof = _configured_contract_proof(
        "no_active_duplicate",
        verdict="REFUTED",
        candidate_only=True,
    )

    assert proof.decision_for("no_active_duplicate").proof_status == "INCOMPLETE"


def test_lifecycle_enum_can_be_llm_normalized_from_source_language() -> None:
    proof = _configured_contract_proof(
        "no_active_duplicate",
        verdict="REFUTED",
        semantic_enum_phrase=True,
    )

    assert proof.decision_for("no_active_duplicate").proof_status == "DISPROVED"


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [("SUPPORTED", "INCOMPLETE"), ("REFUTED", "DISPROVED")],
)
def test_duplicate_counterexample_survives_unconfigured_search_policy(
    verdict: str,
    expected: str,
) -> None:
    proof = _configured_contract_proof(
        "no_active_duplicate",
        verdict=verdict,
        configure_policy=False,
    )

    assert proof.decision_for("no_active_duplicate").proof_status == expected
    if expected == "DISPROVED":
        assert not [
            hole
            for hole in proof.holes
            if "no_active_duplicate" in hole.requirement_ids
        ]


def test_compiled_proof_has_no_claim_or_judgment_mirror() -> None:
    assert "claims" not in CompiledProof.model_fields
    assert "judgments" not in CompiledProof.model_fields


def test_vendor_semantic_gate_proves_only_with_admitted_claim_proposal() -> None:
    evidence, trusted = _vendor_packet()
    proof = compile_evidence_proof(
        [evidence],
        active_requirement_ids={"vendor_identity", "vendor_identity_active"},
        trusted_sources=trusted,
    )

    assert proof.decision_for("vendor_identity").proof_status == "PROVED"
    assert proof.decision_for("vendor_identity_active").proof_status == "PROVED"
    assert {(item.subject, item.predicate) for item in proof.evidence_ir.claims} == {
        ("vendor", "identity"),
        ("vendor", "status"),
        ("vendor", "status_effective_date"),
    }
    assert proof.proposals[0].valid is True


def test_stale_contract_rejects_proposal_without_erasing_admitted_claim() -> None:
    evidence, trusted = _vendor_packet(stale_hash=True)
    proof = compile_evidence_proof(
        [evidence],
        active_requirement_ids={"vendor_identity", "vendor_identity_active"},
        trusted_sources=trusted,
    )

    assert proof.decision_for("vendor_identity_active").proof_status == "INCOMPLETE"
    assert {(item.subject, item.predicate) for item in proof.evidence_ir.claims} == {
        ("vendor", "identity"),
        ("vendor", "status"),
        ("vendor", "status_effective_date"),
    }
    assert [item.code for item in proof.diagnostics if item.candidate_id == "proposal:vendor_identity_active"] == ["STALE_CONTRACT"]


def test_rejected_claim_has_one_primary_lowering_diagnostic() -> None:
    evidence, trusted = _vendor_packet(locator="")
    proof = compile_evidence_proof(
        [evidence],
        active_requirement_ids={"vendor_identity", "vendor_identity_active"},
        trusted_sources=trusted,
    )

    diagnostics = [item for item in proof.diagnostics if item.candidate_id == "CLM_VENDOR_STATUS"]
    assert [item.code for item in diagnostics] == ["LOCATOR_MISSING"]
    assert proof.decision_for("vendor_identity_active").proof_status == "INCOMPLETE"


def test_shared_ir_is_order_stable_and_unrelated_claim_does_not_change_view_hash() -> None:
    vendor, vendor_sources = _vendor_packet()
    base = compile_evidence_proof(
        [vendor],
        active_requirement_ids={"vendor_identity", "vendor_identity_active"},
        trusted_sources=vendor_sources,
    )
    bank, bank_sources = _proposal_packet(
        active_requirement_ids={"vendor_identity", "vendor_bank_change_record", "vendor_bank_account_authorized"},
        requirement_id="vendor_bank_account_authorized",
        source_requirement="vendor_bank_change_record",
        evidence_type="vendor_record",
        evidence_id="ev_bank",
        claim_id="CLM_BANK_AUTHORIZATION",
        subject="vendor_bank",
        predicate="authorization_record",
        typed_value="verified",
        quote="Bank account change authorization verified",
    )
    active = {
        "vendor_identity",
        "vendor_identity_active",
        "vendor_bank_change_record",
        "vendor_bank_account_authorized",
    }
    sources = {**vendor_sources, **bank_sources}
    expanded = compile_evidence_proof(
        [vendor, bank],
        active_requirement_ids=active,
        trusted_sources=sources,
    )
    reordered = compile_evidence_proof(
        [bank, vendor],
        active_requirement_ids=active,
        trusted_sources=dict(reversed(list(sources.items()))),
    )

    assert len(expanded.evidence_ir.claims) == 4
    assert expanded.evidence_ir.model_dump(mode="json") == reordered.evidence_ir.model_dump(mode="json")
    assert {
        item.requirement_id: (item.proof_status, item.evidence_snapshot_hash)
        for item in expanded.decisions
    } == {
        item.requirement_id: (item.proof_status, item.evidence_snapshot_hash)
        for item in reordered.decisions
    }
    assert (
        base.decision_for("vendor_identity_active").evidence_snapshot_hash
        == expanded.decision_for("vendor_identity_active").evidence_snapshot_hash
    )


def test_shared_hole_claim_identity_and_existing_view_hash_survive_new_contract() -> None:
    base = _configured_contract_proof("three_way_amount_match")
    expanded = _configured_contract_proof(
        "three_way_amount_match",
        extra_active={"three_way_quantity_match"},
    )

    def shared_invoice_claim_id(proof: CompiledProof) -> str:
        return next(
            item.id
            for item in proof.evidence_ir.claims
            if item.subject == "invoice" and item.predicate == "order_scope_identity"
        )

    assert shared_invoice_claim_id(base) == shared_invoice_claim_id(expanded)
    assert base.decision_for("three_way_amount_match").proof_status == "PROVED"
    assert expanded.decision_for("three_way_amount_match").proof_status == "PROVED"
    assert (
        base.decision_for("three_way_amount_match").evidence_snapshot_hash
        == expanded.decision_for("three_way_amount_match").evidence_snapshot_hash
    )


def test_empty_user_message_source_cannot_prove_evidence_support() -> None:
    evidence = EvidenceItem(
        id="ev_empty_user_message",
        type="invoice",
        source="user_message",
        credibility="high",
        review_result={"should_accept": True},
        supports=[{"requirement": "invoice", "support_level": "full"}],
        metadata={"classification": "business_evidence"},
    )

    proof = compile_evidence_proof(
        [evidence],
        active_requirement_ids={"invoice"},
        trusted_sources={},
    )

    assert proof.decision_for("invoice").proof_status == "INCOMPLETE"
    assert any(item.code == "SOURCE_NOT_BOUND" for item in proof.diagnostics)


def test_raw_conflict_record_cannot_override_canonical_evidence_proof() -> None:
    quote = "Invoice INV-RAW-1 is the supplied business document"
    evidence = EvidenceItem(
        id="ev_raw_conflict",
        type="invoice",
        source="attachment",
        credibility="high",
        content=quote,
        review_result={"should_accept": True},
        supports=[{"requirement": "invoice", "support_level": "full", "quoted_text": quote}],
        conflicts=[{"requirement": "invoice", "description": "legacy reviewer conflict"}],
        metadata={"classification": "business_evidence"},
    )
    trusted = {
        evidence.id: {
            "texts": [quote],
            "sha256": hashlib.sha256(quote.encode()).hexdigest(),
            "original_ref": "invoice.md",
        }
    }

    proof = compile_evidence_proof(
        [evidence],
        active_requirement_ids={"invoice"},
        trusted_sources=trusted,
    )

    assert proof.decision_for("invoice").proof_status == "PROVED"


def test_one_claim_is_reused_by_two_contract_views_without_duplicate_lowering() -> None:
    evidence, trusted = _vendor_packet()
    vendor_proof = compile_evidence_proof(
        [evidence],
        active_requirement_ids={"vendor_identity", "vendor_identity_active"},
        trusted_sources=trusted,
    )
    status_claim_id = next(
        item.id
        for item in vendor_proof.evidence_ir.claims
        if item.subject == "vendor" and item.predicate == "status"
    )
    pack = deepcopy(REQUIREMENT_PACK)
    pack["bank_change_cooling_period"] = "5 business days"
    pack["unconfigured_policy_values"].remove("bank_change_cooling_period")
    active = {
        "vendor_identity",
        "vendor_identity_active",
        "vendor_bank_change_record",
        "vendor_bank_account_authorized",
    }
    contracts, _ = build_requirement_contracts(active, pack=pack)
    bank_contract = next(item for item in contracts if item.requirement_id == "vendor_bank_account_authorized")
    authorization_quote = "Bank account change authorization verified"
    authorization_claim = {
        "id": "CLM_BANK_AUTHORIZATION",
        "subject": "vendor_bank",
        "predicate": "authorization_record",
        "value_type": "string",
        "typed_value": "verified",
        "source_quote": authorization_quote,
        "source_locator": "vendor.md line 3",
        "confidence": "high",
        "entity_key": "V-42",
    }
    evidence.supports.append(
        evidence.supports[0].model_copy(update={"requirement": "vendor_bank_change_record"})
    )
    evidence.semantic_claims.append(_claim_candidate(bank_contract, authorization_claim))
    evidence.semantic_proposals.append(_proposal_candidate(
        bank_contract,
        verdict="SUPPORTED",
        reason="The source-grounded bank authorization is valid.",
        input_handles=[status_claim_id, "CLM_BANK_AUTHORIZATION"],
    ))
    trusted[evidence.id]["texts"].append(authorization_quote)

    proof = compile_evidence_proof(
        [evidence],
        active_requirement_ids=active,
        trusted_sources=trusted,
        contracts=contracts,
        requirement_pack=pack,
    )
    decisions = [
        proof.decision_for("vendor_identity_active"),
        proof.decision_for("vendor_bank_account_authorized"),
    ]
    root_checks = [
        next(item for item in proof.checks if item.id == decision.root_check_id)
        for decision in decisions
    ]

    assert {(claim.subject, claim.predicate) for claim in proof.evidence_ir.claims} == {
        ("vendor_bank", "authorization_record"),
        ("vendor", "identity"),
        ("vendor", "status"),
        ("vendor", "status_effective_date"),
    }
    assert [decision.proof_status for decision in decisions] == ["PROVED", "PROVED"]
    assert len(root_checks[0].input_claim_ids) == 3
    assert len(root_checks[1].input_claim_ids) == 2


def test_unreferenced_contract_claim_enters_ir_and_blocks_partial_proposal() -> None:
    evidence, trusted = _vendor_packet()
    contradiction_quote = "Vendor V-42 status suspended"
    contract = next(
        item
        for item in build_requirement_contracts({"vendor_identity", "vendor_identity_active"})[0]
        if item.requirement_id == "vendor_identity_active"
    )
    evidence.semantic_claims.append(_claim_candidate(contract, {
        "id": "CLM_VENDOR_STATUS_CONFLICT",
        "subject": "vendor",
        "predicate": "status",
        "entity_key": "V-42",
        "value_type": "enum",
        "typed_value": "suspended",
        "source_quote": contradiction_quote,
        "source_locator": "vendor.md line 4",
        "confidence": "high",
    }))
    trusted[evidence.id]["texts"].append(contradiction_quote)

    proof = compile_evidence_proof(
        [evidence],
        active_requirement_ids={"vendor_identity", "vendor_identity_active"},
        trusted_sources=trusted,
    )

    assert len(proof.evidence_ir.claims) == 4
    assert proof.decision_for("vendor_identity_active").proof_status == "INCOMPLETE"


def test_conflicting_valid_proposals_are_incomplete() -> None:
    evidence, trusted = _vendor_packet()
    contract = next(
        item
        for item in build_requirement_contracts({"vendor_identity", "vendor_identity_active"})[0]
        if item.requirement_id == "vendor_identity_active"
    )
    evidence.semantic_proposals.append({
        **_proposal_candidate(
            contract,
            verdict="REFUTED",
            reason="The same admitted source was interpreted as refuting the gate.",
            input_handles=[
                "CLM_VENDOR_IDENTITY",
                "CLM_VENDOR_STATUS",
                "CLM_VENDOR_STATUS_EFFECTIVE_DATE",
            ],
        ).model_dump(mode="json"),
        "handle": "proposal:vendor_identity_refuted",
    })
    evidence.semantic_proposals = [
        SemanticProposalCandidate.model_validate(item)
        for item in evidence.semantic_proposals
    ]

    proof = compile_evidence_proof(
        [evidence],
        active_requirement_ids={"vendor_identity", "vendor_identity_active"},
        trusted_sources=trusted,
    )
    decision = proof.decision_for("vendor_identity_active")
    root = next(item for item in proof.checks if item.id == decision.root_check_id)

    assert {item.verdict for item in proof.proposals} == {"SUPPORTED", "REFUTED"}
    assert all(item.valid for item in proof.proposals)
    assert decision.proof_status == "INCOMPLETE"
    assert root.reason == "semantic judgments conflict"
    assert any(
        item.kind == "judgment" and "vendor_identity_active" in item.requirement_ids
        for item in proof.holes
    )


def test_unconfigured_policy_keeps_a_supported_proposal_incomplete() -> None:
    active = {"invoice", "invoice_calculation_valid"}
    evidence, trusted = _proposal_packet(
        active_requirement_ids=active,
        requirement_id="invoice_calculation_valid",
        source_requirement="invoice",
        evidence_type="invoice",
        evidence_id="ev_invoice_calculation",
        claim_id="CLM_INVOICE_CALCULATION_TRACE",
        subject="invoice",
        predicate="calculation_trace",
        typed_value="agree",
        quote="Invoice line totals and stated total agree",
    )

    proof = compile_evidence_proof(
        [evidence],
        active_requirement_ids=active,
        trusted_sources=trusted,
    )

    assert proof.proposals[0].valid is True
    assert proof.decision_for("invoice").proof_status == "PROVED"
    assert proof.decision_for("invoice_calculation_valid").proof_status == "INCOMPLETE"
    assert [
        (item.kind, item.policy_key)
        for item in proof.holes
        if item.kind == "policy"
    ] == [("policy", "invoice_calculation_rounding_tolerance")]
    assert any(
        item.operator == "policy_present" and item.status == "INCOMPLETE"
        for item in proof.checks
        if item.requirement_id == "invoice_calculation_valid"
    )
