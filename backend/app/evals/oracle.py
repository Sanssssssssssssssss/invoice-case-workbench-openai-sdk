from __future__ import annotations

from typing import Any

def summarize_case_state(state: Any) -> dict[str, Any]:
    return {
        "case_id": state.case_id,
        "status": state.status,
        "requirements": [{"id": item.id, "status": item.status} for item in state.requirements],
        "evidence_count": len(state.evidence_items),
        "missing_materials": list(state.missing_materials),
        "risk_flags": list(state.risk_flags),
    }


def complete_claim_consistency_errors(case_state: Any) -> list[str]:
    statuses = {item.id: item.status for item in getattr(case_state, "requirements", []) or []}
    requirement_ids = [requirement_id for requirement_id in statuses if requirement_id]
    errors: list[str] = []
    if not requirement_ids:
        errors.append("complete claim conflicts with empty requirements")
    unsatisfied = [
        requirement
        for requirement in sorted(requirement_ids)
        if statuses.get(requirement) not in {"accepted", "satisfied"}
    ]
    if unsatisfied:
        errors.append(f"complete claim conflicts with unsatisfied requirements: {unsatisfied}")
    evidence_items = list(getattr(case_state, "evidence_items", []) or [])
    compiled_proof = getattr(case_state, "compiled_proof", None)
    review_artifact = getattr(case_state, "review_artifact", None)
    evidence_ids = {str(getattr(item, "id", "") or "") for item in evidence_items}
    evidence_ir = getattr(review_artifact, "evidence_ir", None)
    sourced_claim_ids = {
        str(getattr(claim, "id", "") or "")
        for claim in list(getattr(evidence_ir, "claims", []) or [])
        if str(getattr(claim, "source_id", "") or "") in evidence_ids
        and bool(getattr(claim, "quote", ""))
        and bool(getattr(claim, "locator", ""))
    }
    results = {
        str(getattr(item, "node_id", "") or ""): item
        for item in list(getattr(compiled_proof, "node_results", []) or [])
    }
    supported_requirements = {
        str(getattr(decision, "requirement_id", "") or "")
        for decision in list(getattr(compiled_proof, "decisions", []) or [])
        if str(getattr(decision, "status", "")) == "SUPPORTED"
        and (root := results.get(str(getattr(decision, "root_node_id", "") or ""))) is not None
        and str(getattr(root, "status", "")) == "SUPPORTED"
        and (
            bool(getattr(root, "claim_ids", []))
            and set(getattr(root, "claim_ids", [])) <= sourced_claim_ids
            or bool(getattr(root, "source_ids", []))
            and set(getattr(root, "source_ids", [])) <= evidence_ids
        )
    }
    missing_support = sorted(set(requirement_ids) - supported_requirements)
    if missing_support:
        errors.append(f"complete claim missing evidence support for requirements: {missing_support}")
    return errors
