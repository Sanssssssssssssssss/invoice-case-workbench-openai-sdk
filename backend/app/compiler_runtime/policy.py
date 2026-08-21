from __future__ import annotations

import hashlib
import json
from typing import Any, Sequence

from app.compiler_runtime.models import CompiledProof
from app.compiler_runtime.consumer import Reportability, derive_consumer_packet
from app.domain.invoice_requirements import (
    REQUIREMENT_DEFINITIONS,
    REQUIREMENT_PACK,
    REQUIREMENT_PLANNING_HINTS,
    UNCONFIGURED_POLICY_VALUES,
    default_requirement_required,
    requirement_kind,
    requirement_label,
    requirement_owner,
    requirement_premises,
)


def expand_active_requirements(requirement_ids: Sequence[str]) -> list[str]:
    """Close declared premises and activation without building a business proof graph."""

    active = _unique(requirement_ids)
    active_set = set(active)
    changed = True
    while changed:
        changed = False
        for requirement_id in list(active):
            for premise_id in requirement_premises(requirement_id):
                if premise_id not in active_set:
                    active.append(premise_id)
                    active_set.add(premise_id)
                    changed = True
        for requirement_id, hint in REQUIREMENT_PLANNING_HINTS.items():
            if hint.get("activation") != "derived" or requirement_id in active_set:
                continue
            groups = hint.get("activation_requirement_groups") or []
            if groups and all(any(str(item) in active_set for item in group) for group in groups):
                active.append(requirement_id)
                active_set.add(requirement_id)
                changed = True
    return active


def requirement_context(requirement_ids: Sequence[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for requirement_id in requirement_ids:
        definition = REQUIREMENT_DEFINITIONS.get(requirement_id) or {}
        hint = REQUIREMENT_PLANNING_HINTS.get(requirement_id) or {}
        label = requirement_label(requirement_id)
        result.append(
            {
                "id": requirement_id,
                "label": label,
                "proof_target": {"requirement_id": requirement_id, "label": label},
                "kind": requirement_kind(requirement_id),
                "owner": requirement_owner(requirement_id),
                "required": default_requirement_required(requirement_id),
                "premise_requirements": list(definition.get("premise_requirements") or []),
                "required_policy_values": list(definition.get("required_policy_values") or []),
                "capability_hint": str(hint.get("capability") or ""),
                "target_predicate_hint": str(hint.get("target_predicate") or ""),
            }
        )
    return result


def policy_excerpt_for(requirement_ids: Sequence[str]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key in sorted(required_policy_refs(requirement_ids)):
        values[key] = (
            {"configured": False, "value": None}
            if key in UNCONFIGURED_POLICY_VALUES or key not in REQUIREMENT_PACK
            else {"configured": True, "value": REQUIREMENT_PACK[key]}
        )
    return {
        "policy_version": REQUIREMENT_PACK.get("policy_version", ""),
        "policy_basis": REQUIREMENT_PACK.get("policy_basis", {}),
        "values": values,
    }


def policy_hash(policy_excerpt: dict[str, Any]) -> str:
    payload = json.dumps(
        policy_excerpt,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def required_policy_refs(requirement_ids: Sequence[str]) -> set[str]:
    return {
        str(key)
        for requirement_id in requirement_ids
        for key in (REQUIREMENT_DEFINITIONS.get(requirement_id) or {}).get("required_policy_values") or []
    }


def proof_decision_ready(proof: CompiledProof | None) -> bool:
    """Return whether required proof scope has no unresolved obligation.

    This proof-only helper is used while CaseState is being projected.  Report
    generation must use ``case_reportability`` because execution/integrity state
    lives on ReviewArtifact, not CompiledProof.
    """

    return bool(
        proof
        and proof.decisions
        and not any(obligation.blocking for obligation in proof.obligations)
    )


def case_reportability(case_state: Any) -> Reportability:
    return derive_consumer_packet(case_state).reportability


def case_review_complete(case_state: Any) -> bool:
    return derive_consumer_packet(case_state).review_complete


def case_decision_ready(case_state: Any) -> bool:
    return derive_consumer_packet(case_state).decision_ready


def _unique(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


__all__ = [
    "expand_active_requirements",
    "policy_excerpt_for",
    "policy_hash",
    "proof_decision_ready",
    "case_decision_ready",
    "case_reportability",
    "case_review_complete",
    "required_policy_refs",
    "requirement_context",
]
