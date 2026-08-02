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
    supported_requirements = {
        str(getattr(support, "requirement", "") or "")
        for item in evidence_items
        for support in list(getattr(item, "supports", []) or [])
    }
    compiled_proof = getattr(case_state, "compiled_proof", None)
    evidence_ids = {str(getattr(item, "id", "") or "") for item in evidence_items}
    sourced_claim_ids = {
        str(getattr(claim, "id", "") or "")
        for claim in list(getattr(compiled_proof, "claims", []) or [])
        if str(getattr(claim, "evidence_id", "") or "") in evidence_ids
        and bool(getattr(claim, "source_quote", ""))
        and bool(getattr(claim, "source_locator", ""))
    }
    roots = {
        (str(getattr(decision, "program_id", "") or ""), str(getattr(decision, "root_check_id", "") or ""))
        for decision in list(getattr(compiled_proof, "decisions", []) or [])
    }
    supported_requirements.update(
        str(getattr(check, "requirement_id", "") or "")
        for check in list(getattr(compiled_proof, "checks", []) or [])
        if (str(getattr(check, "program_id", "") or ""), str(getattr(check, "id", "") or "")) in roots
        and str(getattr(check, "status", "")) == "PROVED"
        and bool(getattr(check, "input_claim_ids", []))
        and set(getattr(check, "input_claim_ids", [])) <= sourced_claim_ids
    )
    missing_support = sorted(set(requirement_ids) - supported_requirements)
    if missing_support:
        errors.append(f"complete claim missing evidence support for requirements: {missing_support}")
    return errors
