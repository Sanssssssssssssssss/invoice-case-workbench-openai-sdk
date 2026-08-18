from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from app.domain.invoice_requirements import REQUIREMENT_PACK
from app.state.schemas import (
    CaseEvidenceIR,
    ContractInput,
    ProofProposal,
    Requirement,
    RequirementContract,
    TypedHole,
)


def build_requirement_contracts(
    requirements: Iterable[Requirement | str],
    *,
    evidence_ir: CaseEvidenceIR | None = None,
    proposals: Iterable[ProofProposal] = (),
    pack: Mapping[str, Any] = REQUIREMENT_PACK,
) -> tuple[list[RequirementContract], list[TypedHole]]:
    """Build the active policy contracts and only their unresolved typed holes."""
    definitions = pack.get("requirements") or {}
    configured_contracts = pack.get("proof_contracts") or {}
    explicit = {
        item if isinstance(item, str) else item.id
        for item in requirements
    }
    unknown = explicit - set(definitions)
    if unknown:
        raise ValueError(f"Unknown requirement ids: {', '.join(sorted(unknown))}")

    active = {
        requirement_id
        for requirement_id in explicit
        if (configured_contracts.get(requirement_id) or {}).get("activation") != "derived"
    }
    changed = True
    while changed:
        changed = False
        for requirement_id, config in configured_contracts.items():
            if requirement_id in active or config.get("activation") != "derived":
                continue
            groups = config.get("activation_requirement_groups") or []
            if groups and all(any(item in active for item in group) for group in groups):
                active.add(requirement_id)
                changed = True

    contracts = [
        _build_contract(requirement_id, definitions[requirement_id], configured_contracts.get(requirement_id) or {}, pack)
        for requirement_id in sorted(active)
    ]
    ir = evidence_ir or CaseEvidenceIR()
    proposal_rows = list(proposals)
    holes: dict[str, TypedHole] = {}
    for contract in contracts:
        for item in contract.inputs:
            if _input_resolved(item, contract, ir, proposal_rows):
                continue
            if not item.required:
                continue
            _add_hole(
                holes,
                contract,
                kind=item.hole_kind,
                semantic_key=_semantic_key(item, contract),
                predicate=item.predicate,
                subject=item.subject,
                evidence_roles=[item.role] if item.role else [],
                reason=f"Required {item.hole_kind} input is unresolved.",
            )
        for policy_key in contract.policy_inputs:
            if _policy_configured(pack, policy_key):
                continue
            _add_hole(
                holes,
                contract,
                kind="policy",
                semantic_key=f"policy:{policy_key}",
                policy_key=policy_key,
                reason="The enterprise policy value is not configured.",
            )
    return contracts, sorted(holes.values(), key=lambda item: item.semantic_key)


def _build_contract(
    requirement_id: str,
    definition: Mapping[str, Any],
    config: Mapping[str, Any],
    pack: Mapping[str, Any],
) -> RequirementContract:
    authority = "evidence" if definition.get("owner") == "evidence" else "compiler"
    proof_template = str(config.get("proof_template") or ("evidence_support" if authority == "evidence" else "semantic_gate"))
    target_predicate = str(config.get("target_predicate") or f"evidence.supports.{requirement_id}")
    inputs = [
        ContractInput.model_validate(_contract_input(requirement_id, proof_template, item, index))
        for index, item in enumerate(config.get("inputs") or [])
    ]
    roles = list(
        config.get("evidence_roles")
        or definition.get("premise_requirements")
        or (
            [requirement_id]
            if authority == "evidence"
            else dict.fromkeys(item.role for item in inputs if item.role and item.hole_kind != "judgment")
        )
    )
    inputs.extend(
        ContractInput(
            slot_id=f"{requirement_id}:source:{role}",
            predicate=f"evidence.supports.{role}",
            role=str(role),
            hole_kind="source",
        )
        for role in roles
        if not any(item.hole_kind == "source" and item.role == role for item in inputs)
    )
    if proof_template != "evidence_support" and not any(item.hole_kind == "judgment" for item in inputs):
        inputs.append(ContractInput(
            slot_id=f"{requirement_id}:judgment:{target_predicate}",
            predicate=target_predicate,
            role=requirement_id,
            hole_kind="judgment",
        ))

    version = str(pack.get("contract_version") or pack.get("requirement_pack_version") or "1")
    policy_inputs = [str(item) for item in definition.get("required_policy_values") or []]
    contract_id = f"contract:{requirement_id}:{version}"
    payload = {
        "contract_id": contract_id,
        "version": version,
        "requirement_id": requirement_id,
        "proof_template": proof_template,
        "target_predicate": target_predicate,
        "inputs": [item.model_dump(mode="json") for item in inputs],
        "evidence_roles": roles,
        "policy_inputs": policy_inputs,
        "policy_values": {key: pack.get(key, "__UNCONFIGURED__") for key in policy_inputs},
        "capability": str(config.get("capability") or proof_template),
        "activation": str(config.get("activation") or ("derived" if definition.get("owner") == "compiler" else "explicit")),
        "candidate_actions": [str(item) for item in config.get("candidate_actions") or []],
    }
    return RequirementContract(
        **{key: value for key, value in payload.items() if key != "policy_values"},
        contract_hash=_hash(payload),
    )


def _input_resolved(
    item: ContractInput,
    contract: RequirementContract,
    ir: CaseEvidenceIR,
    proposals: list[ProofProposal],
) -> bool:
    if item.hole_kind == "source":
        return any(
            binding.trusted
            and binding.accepted
            and binding.support_levels.get(item.role) == "full"
            for binding in ir.source_bindings
        )
    if item.hole_kind in {"claim", "relation"}:
        bindings_by_id = {binding.evidence_id: binding for binding in ir.source_bindings}
        return any(
            claim.predicate == item.predicate
            and (not item.subject or claim.subject == item.subject)
            and (not item.value_type or claim.value_type == item.value_type)
            and (
                not item.allowed_values
                or _value_key(claim.typed_value) in {_value_key(value) for value in item.allowed_values}
            )
            and all(
                getattr(claim, attribute, None) not in {None, ""}
                and attribute in claim.attribute_sources
                for attribute in item.required_attributes
            )
            and (
                not item.role
                or (
                    claim.evidence_id in bindings_by_id
                    and bindings_by_id[claim.evidence_id].trusted
                    and bindings_by_id[claim.evidence_id].accepted
                    and bindings_by_id[claim.evidence_id].support_levels.get(item.role, "none") != "none"
                )
            )
            for claim in ir.claims
        )
    if item.hole_kind == "judgment":
        return any(
            proposal.valid
            and proposal.contract_id == contract.contract_id
            and proposal.contract_hash == contract.contract_hash
            and proposal.target_predicate == item.predicate
            and proposal.confidence == "high"
            and not proposal.open_questions
            and bool(proposal.input_refs)
            and all(
                ref.claim_id and ref.evidence_id and ref.source_quote and ref.source_locator
                for ref in proposal.input_refs
            )
            and (
                proposal.verdict == "SUPPORTED"
                and bool(proposal.supporting_refs)
                and not proposal.opposing_refs
                or proposal.verdict == "REFUTED"
                and bool(proposal.opposing_refs)
            )
            and proposal.evidence_snapshot_hash == ir.source_snapshot_hash
            for proposal in proposals
        )
    return False


def _semantic_key(item: ContractInput, contract: RequirementContract) -> str:
    if item.hole_kind == "source":
        return f"source:{item.role}"
    if item.hole_kind == "judgment":
        return f"judgment:{contract.contract_hash}:{item.predicate}"
    return f"{item.hole_kind}:{item.subject}:{item.predicate}:{item.binding_mode}:{item.binding_group}"


def contract_hole_id(item: ContractInput, contract: RequirementContract) -> str:
    return f"hole:{_hash(_semantic_key(item, contract))[:16]}"


def _add_hole(
    holes: dict[str, TypedHole],
    contract: RequirementContract,
    *,
    kind: str,
    semantic_key: str,
    predicate: str = "",
    subject: str = "",
    policy_key: str = "",
    evidence_roles: list[str] | None = None,
    reason: str,
) -> None:
    existing = holes.get(semantic_key)
    if existing:
        existing.contract_ids = sorted(set(existing.contract_ids + [contract.contract_id]))
        existing.requirement_ids = sorted(set(existing.requirement_ids + [contract.requirement_id]))
        existing.evidence_roles = sorted(set(existing.evidence_roles + (evidence_roles or [])))
        existing.slot_ids = sorted(set(existing.slot_ids + [item.slot_id for item in contract.inputs if _semantic_key(item, contract) == semantic_key]))
        return
    source_input = next((item for item in contract.inputs if _semantic_key(item, contract) == semantic_key), None)
    holes[semantic_key] = TypedHole(
        id=f"hole:{_hash(semantic_key)[:16]}",
        kind=kind,
        semantic_key=semantic_key,
        slot_ids=[source_input.slot_id] if source_input else [],
        contract_ids=[contract.contract_id],
        requirement_ids=[contract.requirement_id],
        predicate=predicate,
        subject=subject,
        policy_key=policy_key,
        evidence_roles=evidence_roles or [],
        value_type=source_input.value_type if source_input else "",
        allowed_values=list(source_input.allowed_values) if source_input else [],
        required_attributes=list(source_input.required_attributes) if source_input else [],
        binding_mode=source_input.binding_mode if source_input else "global",
        binding_group=source_input.binding_group if source_input else "",
        reason=reason,
    )


def _contract_input(requirement_id: str, proof_template: str, raw: Mapping[str, Any], index: int) -> dict[str, Any]:
    data = dict(raw)
    kind = str(data.get("hole_kind") or "claim")
    if "binding_mode" not in data:
        data["binding_mode"] = (
            "global"
            if kind not in {"claim", "relation"} or proof_template == "entity_lifecycle"
            else "singleton_by_role"
        )
    data.setdefault(
        "slot_id",
        ":".join((
            requirement_id,
            kind,
            str(data.get("subject") or data.get("role") or "input"),
            str(data.get("predicate") or index),
        )),
    )
    return data


def _hash(value: Any) -> str:
    payload = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _value_key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _policy_configured(pack: Mapping[str, Any], key: str) -> bool:
    if key in set(pack.get("unconfigured_policy_values") or []) or key not in pack:
        return False
    value = pack[key]
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


__all__ = ["build_requirement_contracts", "contract_hole_id"]
