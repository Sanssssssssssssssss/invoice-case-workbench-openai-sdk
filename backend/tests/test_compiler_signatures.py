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
        version="1",
        requirement_id=REQUIREMENT_ID,
        root_composition="ALL_REQUIRED",
        required_policy_refs=[POLICY_REF],
        facets=[
            ProofFacet(id="line_extensions", minimum_proof_terms=["WITNESS"]),
            ProofFacet(id="subtotal_aggregation", minimum_proof_terms=["WITNESS"]),
            ProofFacet(id="stated_components", minimum_proof_terms=["BINDING", "WITNESS"]),
            ProofFacet(id="final_total", minimum_proof_terms=["WITNESS"]),
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
                facet_refs=["line_extensions"],
            ),
            ProofNode(
                id="check.subtotal",
                kind="CHECK",
                statement="The line extensions aggregate to the stated subtotal.",
                requirement_refs=[REQUIREMENT_ID],
                facet_refs=["subtotal_aggregation"],
            ),
            ProofNode(
                id="check.components",
                kind="CHECK",
                statement="Each stated component has a supported basis and arithmetic.",
                requirement_refs=[REQUIREMENT_ID],
                facet_refs=["stated_components"],
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
    assert all(set(facet.model_dump()) == {"id", "minimum_proof_terms"} for facet in signature.facets)


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

    with pytest.raises(ValueError, match="facet fields"):
        load_requirement_pack(path)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda plan: setattr(
                next(node for node in plan.nodes if node.id == "check.components"),
                "facet_refs",
                [],
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
            "ALL_REQUIRED",
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
    shared.policy_refs.append(POLICY_REF)
    plan.nodes = [node for node in plan.nodes if node.id != "check.final"]
    root = next(node for node in plan.nodes if node.id == "root.calculation")
    root.depends_on.remove("check.final")
    plan = ProofPlan.model_validate(plan.model_dump())

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
