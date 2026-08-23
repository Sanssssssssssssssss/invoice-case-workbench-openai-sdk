from __future__ import annotations

from copy import deepcopy
import json

import pytest

from app.compiler_runtime.models import ProofNode, ProofPlan
from app.compiler_runtime.signatures import (
    PlanConformanceGate,
    ProofFacet,
    ProofSignature,
    proof_signature_for,
    proof_signature_hash_for,
)
from app.domain.invoice_requirements import REQUIREMENT_PACK, load_requirement_pack


REQUIREMENT_ID = "invoice_calculation_valid"
POLICY_REF = "invoice_calculation_rounding_tolerance"
TEMPLATE_REQUIREMENT_ID = "template_match"
TEMPLATE_POLICY_REF = "invoice_template_baseline_ref"


def _signature() -> ProofSignature:
    return ProofSignature(
        signature_id="invoice_arithmetic",
        version="2",
        requirement_id=REQUIREMENT_ID,
        root_composition="ALL_REQUIRED",
        required_policy_refs=[POLICY_REF],
        facets=[
            ProofFacet(
                id="line_extensions",
                minimum_proof_terms=["WITNESS"],
                semantic_contract=(
                    "Enumerate every source-listed item, service, or detail amount that contributes "
                    "to the subtotal. For every entry that explicitly states quantity, unit price, "
                    "and extension, ground all three and replay the multiplication before accepting "
                    "its extension. A lump-sum entry remains a line extension even when quantity or "
                    "unit price is absent: ground its stated extension without inventing a "
                    "multiplication. For this facet, complete coverage means one grounded extension "
                    "Claim per source-listed entry; arithmetic inclusion exactly once in a SUM "
                    "belongs to the subtotal_aggregation facet. No entry may be omitted or "
                    "reclassified as an applied component merely from its label. If an entry's "
                    "arithmetic role is ambiguous or an explicit calculation is not replayed, "
                    "preserve the outcome as NOT_FOUND."
                ),
            ),
            ProofFacet(
                id="subtotal_aggregation",
                minimum_proof_terms=["WITNESS"],
                semantic_contract=(
                    "Recompute the subtotal from every source-listed line extension established "
                    "for the document, including grounded lump-sum entries, and compare that "
                    "derived subtotal with the stated subtotal. An omitted or ambiguously "
                    "classified subtotal input is a coverage gap, not evidence that the stated "
                    "subtotal is contradicted."
                ),
            ),
            ProofFacet(
                id="stated_components",
                minimum_proof_terms=["BINDING", "WITNESS"],
                semantic_contract=(
                    "Verify each source-stated tax, discount, charge, or other adjustment that "
                    "source context presents as an applied component outside the item, service, "
                    "or detail listing. Do not reclassify a listed subtotal input as a component "
                    "merely from its label, and do not classify one source amount as both a line "
                    "extension and an applied component. The component amount and sign/role must "
                    "be grounded in the source. When a rate-based calculation is claimed, both the "
                    "rate and its applicable base relationship must be grounded; numerical "
                    "coincidence cannot establish the base. COMPONENT_RECONCILIATION means "
                    "computing the component amount, comparing it with the stated component amount, "
                    "and applying tolerance; inclusion in the final total belongs to the final_total "
                    "facet and is not a prerequisite here. If the applicable base or arithmetic role "
                    "cannot be established, preserve the component-validity outcome as NOT_FOUND."
                ),
                required_semantic_roles=[
                    "COMPONENT_OBSERVATION",
                    "COMPONENT_APPLICABILITY",
                    "COMPONENT_RECONCILIATION",
                ],
            ),
            ProofFacet(
                id="final_total",
                minimum_proof_terms=["WITNESS"],
                semantic_contract=(
                    "Reconcile the stated final total from independently established upstream "
                    "amounts while preserving every source-stated sign. When the source states a "
                    "transaction-document type or subtype that determines sign semantics, ground "
                    "that document type and include it in the final-total proof; never infer "
                    "polarity from convention alone."
                ),
            ),
        ],
    )


def _plan() -> ProofPlan:
    return ProofPlan(
        plan_id="invoice-calculation",
        objective="Verify the invoice's internal arithmetic.",
        active_requirement_ids=[REQUIREMENT_ID],
        policy_refs=[POLICY_REF],
        roots={REQUIREMENT_ID: "root.calculation"},
        nodes=[
            ProofNode(
                id="check.lines",
                kind="CHECK",
                statement="Each stated line extension is internally consistent.",
                requirement_refs=[REQUIREMENT_ID],
                policy_refs=[POLICY_REF],
                facet_refs=["line_extensions"],
            ),
            ProofNode(
                id="check.subtotal",
                kind="CHECK",
                statement="The line extensions aggregate to the stated subtotal.",
                requirement_refs=[REQUIREMENT_ID],
                policy_refs=[POLICY_REF],
                facet_refs=["subtotal_aggregation"],
            ),
            ProofNode(
                id="check.components",
                kind="CHECK",
                statement="Each stated component has a supported basis and arithmetic.",
                requirement_refs=[REQUIREMENT_ID],
                policy_refs=[POLICY_REF],
                facet_refs=["stated_components"],
                semantic_role_refs=[
                    "COMPONENT_OBSERVATION",
                    "COMPONENT_APPLICABILITY",
                    "COMPONENT_RECONCILIATION",
                ],
            ),
            ProofNode(
                id="check.final",
                kind="CHECK",
                statement="The independently derived total agrees with the stated total.",
                requirement_refs=[REQUIREMENT_ID],
                policy_refs=[POLICY_REF],
                facet_refs=["final_total"],
            ),
            ProofNode(
                id="root.calculation",
                kind="ALL",
                depends_on=["check.lines", "check.subtotal", "check.components", "check.final"],
            ),
        ],
    )


def _template_signature() -> ProofSignature:
    return ProofSignature(
        signature_id="invoice_template_comparison",
        version="1",
        requirement_id=TEMPLATE_REQUIREMENT_ID,
        root_composition="ALL_REQUIRED",
        required_policy_refs=[TEMPLATE_POLICY_REF],
        facets=[
            ProofFacet(id="baseline_comparison", minimum_proof_terms=["BINDING"]),
        ],
    )


def _template_plan() -> ProofPlan:
    return ProofPlan(
        plan_id="invoice-template-comparison",
        objective="Compare the invoice with the selected expected template baseline.",
        active_requirement_ids=[TEMPLATE_REQUIREMENT_ID],
        policy_refs=[TEMPLATE_POLICY_REF],
        roots={TEMPLATE_REQUIREMENT_ID: "check.template"},
        nodes=[
            ProofNode(
                id="check.template",
                kind="CHECK",
                statement="The invoice conforms to the selected expected template baseline.",
                requirement_refs=[TEMPLATE_REQUIREMENT_ID],
                policy_refs=[TEMPLATE_POLICY_REF],
                facet_refs=["baseline_comparison"],
            )
        ],
    )


def test_pack_exposes_the_minimal_invoice_calculation_signature() -> None:
    signature = proof_signature_for(REQUIREMENT_ID)

    assert signature is not None
    assert signature == _signature()
    assert set(signature.model_dump()) == {
        "signature_id",
        "version",
        "requirement_id",
        "root_composition",
        "required_policy_refs",
        "facets",
    }
    assert all(
        set(facet.model_dump())
        == {"id", "minimum_proof_terms", "semantic_contract", "required_semantic_roles"}
        for facet in signature.facets
    )
    components = next(facet for facet in signature.facets if facet.id == "stated_components")
    lines = next(facet for facet in signature.facets if facet.id == "line_extensions")
    subtotal = next(facet for facet in signature.facets if facet.id == "subtotal_aggregation")
    final_total = next(facet for facet in signature.facets if facet.id == "final_total")
    assert components.required_semantic_roles == [
        "COMPONENT_OBSERVATION",
        "COMPONENT_APPLICABILITY",
        "COMPONENT_RECONCILIATION",
    ]
    assert "numerical coincidence cannot establish the base" in components.semantic_contract
    assert "inclusion in the final total belongs to the final_total facet" in components.semantic_contract
    assert "lump-sum entry remains a line extension" in lines.semantic_contract
    assert "one grounded extension Claim per source-listed entry" in lines.semantic_contract
    assert "belongs to the subtotal_aggregation facet" in lines.semantic_contract
    assert "coverage gap, not evidence" in subtotal.semantic_contract
    assert "transaction-document type or subtype" in final_total.semantic_contract


def test_pack_exposes_a_tiny_template_baseline_signature() -> None:
    signature = proof_signature_for(TEMPLATE_REQUIREMENT_ID)

    assert signature == _template_signature()
    assert len(signature.facets) == 1
    assert signature.facets[0].minimum_proof_terms == ["BINDING"]


@pytest.mark.parametrize("missing", ["facet", "policy"])
def test_template_signature_requires_a_baseline_binding_and_policy_lineage(missing: str) -> None:
    plan = _template_plan()
    check = plan.nodes[0]
    if missing == "facet":
        check.facet_refs = []
    else:
        plan.policy_refs = []
        check.policy_refs = []

    with pytest.raises(ValueError, match="baseline_comparison|invoice_template_baseline_ref"):
        PlanConformanceGate([_template_signature()]).validate(plan)


def test_signature_schema_rejects_business_rule_dsl_fields() -> None:
    payload = _signature().model_dump()
    payload["facets"][0]["formula"] = "quantity * unit_price"

    with pytest.raises(ValueError, match="formula"):
        ProofSignature.model_validate(payload)


@pytest.mark.parametrize(
    "missing_role",
    [
        "COMPONENT_OBSERVATION",
        "COMPONENT_APPLICABILITY",
        "COMPONENT_RECONCILIATION",
    ],
)
def test_component_amount_presence_cannot_bypass_required_semantic_roles(
    missing_role: str,
) -> None:
    plan = _plan()
    component = next(node for node in plan.nodes if node.id == "check.components")
    component.semantic_role_refs.remove(missing_role)

    with pytest.raises(ValueError, match=missing_role):
        PlanConformanceGate([_signature()]).validate(plan)


def test_component_semantic_roles_cannot_be_split_across_checks() -> None:
    plan = _plan()
    root = next(node for node in plan.nodes if node.id == "root.calculation")
    component = next(node for node in plan.nodes if node.id == "check.components")
    component.semantic_role_refs = ["COMPONENT_OBSERVATION"]
    for suffix, role in (
        ("applicability", "COMPONENT_APPLICABILITY"),
        ("reconciliation", "COMPONENT_RECONCILIATION"),
    ):
        node_id = f"check.components.{suffix}"
        plan.nodes.append(
            ProofNode(
                id=node_id,
                kind="CHECK",
                statement=f"The stated component {suffix} is independently verified.",
                requirement_refs=[REQUIREMENT_ID],
                policy_refs=[POLICY_REF],
                facet_refs=["stated_components"],
                semantic_role_refs=[role],
            )
        )
        root.depends_on.append(node_id)

    with pytest.raises(ValueError, match="must declare every required semantic role"):
        PlanConformanceGate([_signature()]).validate(ProofPlan.model_validate(plan.model_dump()))


def test_signature_hash_is_order_independent_and_tracks_the_empty_set() -> None:
    active = proof_signature_hash_for(["invoice", REQUIREMENT_ID])

    assert active == proof_signature_hash_for([REQUIREMENT_ID, "invoice"])
    assert active != proof_signature_hash_for(["invoice"])
    assert proof_signature_hash_for([]) == proof_signature_hash_for(["invoice"])


def test_requirement_pack_rejects_business_rule_dsl_fields_in_signatures(tmp_path) -> None:
    pack = deepcopy(REQUIREMENT_PACK)
    pack["proof_signatures"][0]["facets"][0]["formula"] = "quantity * unit_price"
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(pack), encoding="utf-8")

    with pytest.raises(ValueError, match="formula|Extra inputs"):
        load_requirement_pack(path)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda plan: (
                setattr(
                    next(node for node in plan.nodes if node.id == "check.components"),
                    "facet_refs",
                    [],
                ),
                setattr(
                    next(node for node in plan.nodes if node.id == "check.components"),
                    "semantic_role_refs",
                    [],
                ),
            ),
            "stated_components",
        ),
        (
            lambda plan: (
                plan.active_requirement_ids.append("invoice"),
                plan.roots.__setitem__("invoice", "check.lines"),
                next(
                    node for node in plan.nodes if node.id == "root.calculation"
                ).depends_on.remove("check.lines"),
                setattr(
                    next(node for node in plan.nodes if node.id == "check.lines"),
                    "requirement_refs",
                    ["invoice"],
                ),
            ),
            "line_extensions.*reachable",
        ),
        (
            lambda plan: setattr(
                next(node for node in plan.nodes if node.id == "root.calculation"),
                "kind",
                "ANY",
            ),
            "ALL_REQUIRED|can bypass semantic role",
        ),
        (
            lambda plan: (
                setattr(plan, "policy_refs", []),
                setattr(
                    next(node for node in plan.nodes if node.id == "check.final"),
                    "policy_refs",
                    [],
                ),
            ),
            POLICY_REF,
        ),
    ],
)
def test_conformance_rejects_missing_unreachable_bypassed_or_unconfigured_terms(
    mutate,
    message: str,
) -> None:
    payload = _plan().model_dump()
    plan = ProofPlan.model_construct(**deepcopy(payload))
    plan.nodes = [ProofNode.model_construct(**node) for node in payload["nodes"]]
    mutate(plan)

    with pytest.raises(ValueError, match=message):
        PlanConformanceGate([_signature()]).validate(plan)


def test_conformance_allows_a_facet_to_expand_into_multiple_checks() -> None:
    plan = _plan()
    root = next(node for node in plan.nodes if node.id == "root.calculation")
    plan.nodes = [node for node in plan.nodes if node.id != "check.lines"]
    root.depends_on.remove("check.lines")
    for suffix in ("one", "two"):
        node_id = f"check.line.{suffix}"
        plan.nodes.append(
            ProofNode(
                id=node_id,
                kind="CHECK",
                statement=f"Line group {suffix} is internally consistent.",
                requirement_refs=[REQUIREMENT_ID],
                policy_refs=[POLICY_REF],
                facet_refs=["line_extensions"],
            )
        )
        root.depends_on.append(node_id)
    plan = ProofPlan.model_validate(plan.model_dump())

    PlanConformanceGate([_signature()]).validate(plan)


def test_conformance_allows_one_check_to_cover_multiple_facets() -> None:
    plan = _plan()
    shared = next(node for node in plan.nodes if node.id == "check.components")
    shared.facet_refs.append("final_total")
    plan.nodes = [node for node in plan.nodes if node.id != "check.final"]
    root = next(node for node in plan.nodes if node.id == "root.calculation")
    root.depends_on.remove("check.final")
    plan = ProofPlan.model_validate(plan.model_dump())

    PlanConformanceGate([_signature()]).validate(plan)


def test_conformance_rejects_witness_facet_without_its_required_policy() -> None:
    plan = _plan()
    next(node for node in plan.nodes if node.id == "check.subtotal").policy_refs = []

    with pytest.raises(ValueError, match="subtotal_aggregation.*required policy refs"):
        PlanConformanceGate([_signature()]).validate(plan)


def test_conformance_allows_the_compiler_to_add_facets_beyond_the_signature() -> None:
    plan = _plan()
    plan.nodes.append(
        ProofNode(
            id="check.additional-risk",
            kind="CHECK",
            statement="An additional document-specific arithmetic risk is checked.",
            requirement_refs=[REQUIREMENT_ID],
            facet_refs=["compiler_discovered_risk"],
        )
    )
    root = next(node for node in plan.nodes if node.id == "root.calculation")
    root.depends_on.append("check.additional-risk")
    plan = ProofPlan.model_validate(plan.model_dump())

    PlanConformanceGate([_signature()]).validate(plan)
