from __future__ import annotations

import json
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
POLICY_PATH = PROJECT_ROOT / "policies" / "aurora_ap_policy_v1.json"
_KINDS = {"document", "field", "cross_check", "visual", "risk_check"}
_OWNERS = {"evidence", "reviewer", "compiler"}
_PROOF_TEMPLATES = {"evidence_support", "semantic_gate", "reconciliation", "entity_lifecycle"}
_CONTRACT_ACTIVATIONS = {"explicit", "derived"}
_HOLE_KINDS = {"source", "claim", "relation", "judgment", "policy"}
_BINDING_MODES = {"global", "singleton_by_role", "same_entity", "per_entity"}
_CLAIM_VALUE_TYPES = {"", "boolean", "currency", "date", "decimal", "enum", "integer", "string"}
_CLAIM_ATTRIBUTES = {"unit", "currency", "basis", "tax_basis", "coverage"}


def load_requirement_pack(path: Path = POLICY_PATH) -> dict[str, Any]:
    pack = json.loads(path.read_text(encoding="utf-8"))
    requirements = pack.get("requirements")
    profiles = pack.get("profiles")
    if not isinstance(requirements, dict) or not isinstance(profiles, dict):
        raise ValueError(f"Invalid requirement pack: {path}")
    contract_version = pack.get("contract_version", pack.get("requirement_pack_version"))
    proof_contracts = pack.get("proof_contracts", {})
    if proof_contracts is None:
        proof_contracts = {}
    if not str(contract_version or "").strip() or not isinstance(proof_contracts, dict):
        raise ValueError("Invalid proof contracts")
    raw_unconfigured = pack.get("unconfigured_policy_values") or []
    if not isinstance(raw_unconfigured, list) or any(not str(item).strip() for item in raw_unconfigured):
        raise ValueError("Invalid unconfigured_policy_values")
    unconfigured = {str(item) for item in raw_unconfigured}
    if unconfigured.intersection(pack):
        raise ValueError("Policy values cannot be both configured and unconfigured")
    for requirement_id, definition in requirements.items():
        if not isinstance(definition, dict) or definition.get("kind") not in _KINDS or definition.get("owner") not in _OWNERS:
            raise ValueError(f"Invalid requirement definition: {requirement_id}")
        premises = definition.get("premise_requirements") or []
        if not isinstance(premises, list) or any(str(item) not in requirements for item in premises):
            raise ValueError(f"Invalid requirement premises: {requirement_id}")
        if requirement_id in premises:
            raise ValueError(f"Self-referencing requirement premise: {requirement_id}")
        policy_values = definition.get("required_policy_values") or []
        if (
            not isinstance(policy_values, list)
            or any(not str(item).strip() for item in policy_values)
            or any(str(item) not in pack and str(item) not in unconfigured for item in policy_values)
        ):
            raise ValueError(f"Invalid requirement policy values: {requirement_id}")
        if definition.get("owner") == "reviewer" and any(
            requirements[str(item)].get("owner") != "evidence" for item in premises
        ):
            raise ValueError(f"Reviewer premises must be evidence-owned: {requirement_id}")
    for profile_id, rows in profiles.items():
        if not isinstance(rows, list) or any(not isinstance(row, dict) or row.get("id") not in requirements for row in rows):
            raise ValueError(f"Invalid requirement profile: {profile_id}")
    for requirement_id, contract in proof_contracts.items():
        if requirement_id not in requirements or not isinstance(contract, dict):
            raise ValueError(f"Invalid proof contract: {requirement_id}")
        if contract.get("proof_template") not in _PROOF_TEMPLATES:
            raise ValueError(f"Invalid proof template: {requirement_id}")
        if contract.get("activation", "explicit") not in _CONTRACT_ACTIVATIONS:
            raise ValueError(f"Invalid proof contract activation: {requirement_id}")
        owner = requirements[requirement_id].get("owner")
        expected_activation = "derived" if owner == "compiler" else "explicit"
        if contract.get("activation", "explicit") != expected_activation:
            raise ValueError(f"Invalid proof contract authority: {requirement_id}")
        if not str(contract.get("target_predicate") or "").strip():
            raise ValueError(f"Invalid proof target predicate: {requirement_id}")
        inputs = contract.get("inputs") or []
        if not isinstance(inputs, list) or any(
            not isinstance(item, dict)
            or not str(item.get("predicate") or "").strip()
            or item.get("hole_kind", "claim") not in _HOLE_KINDS
            or str(item.get("value_type") or "") not in _CLAIM_VALUE_TYPES
            or not isinstance(item.get("allowed_values") or [], list)
            or any(not str(value).strip() for value in item.get("allowed_values") or [])
            or not isinstance(item.get("required_attributes") or [], list)
            or any(str(value) not in _CLAIM_ATTRIBUTES for value in item.get("required_attributes") or [])
            or (item.get("hole_kind") in {"claim", "relation"} and not str(item.get("subject") or "").strip())
            or (item.get("hole_kind") in {"claim", "relation"} and not str(item.get("role") or "").strip())
            or (item.get("hole_kind") in {"claim", "relation"} and not str(item.get("value_type") or "").strip())
            or (
                item.get("hole_kind") in {"claim", "relation"}
                and requirements.get(str(item.get("role") or ""), {}).get("owner") != "evidence"
            )
            or (item.get("role") and str(item.get("role")) not in requirements)
            or item.get("binding_mode", "") not in {"", *_BINDING_MODES}
            or (
                item.get("binding_mode") in {"same_entity", "per_entity"}
                and not str(item.get("binding_group") or "").strip()
            )
            for item in inputs
        ):
            raise ValueError(f"Invalid proof contract inputs: {requirement_id}")
        groups = contract.get("activation_requirement_groups") or []
        if not isinstance(groups, list) or any(
            not isinstance(group, list)
            or not group
            or any(str(item) not in requirements or str(item) == requirement_id for item in group)
            for group in groups
        ):
            raise ValueError(f"Invalid proof contract activation requirements: {requirement_id}")
        if expected_activation == "derived" and not groups:
            raise ValueError(f"Derived proof contract has no activation requirements: {requirement_id}")
        actions = contract.get("candidate_actions") or []
        if not isinstance(actions, list) or any(not str(item).strip() for item in actions):
            raise ValueError(f"Invalid proof contract actions: {requirement_id}")
    missing_contracts = {
        requirement_id
        for requirement_id, definition in requirements.items()
        if definition.get("owner") != "evidence" and requirement_id not in proof_contracts
    }
    if missing_contracts:
        raise ValueError(f"Missing proof contracts: {', '.join(sorted(missing_contracts))}")
    input_schemas: dict[tuple[str, str], tuple[Any, ...]] = {}
    for requirement_id, contract in proof_contracts.items():
        for item in contract.get("inputs") or []:
            if item.get("hole_kind") not in {"claim", "relation"}:
                continue
            key = (str(item["subject"]), str(item["predicate"]))
            schema = (
                str(item["role"]),
                str(item["value_type"]),
                tuple(sorted(str(value) for value in item.get("allowed_values") or [])),
                tuple(sorted(str(value) for value in item.get("required_attributes") or [])),
                str(item.get("binding_mode") or ""),
                str(item.get("binding_group") or ""),
            )
            if key in input_schemas and input_schemas[key] != schema:
                raise ValueError(f"Conflicting proof input schema: {key[0]}.{key[1]}")
            input_schemas[key] = schema
    try:
        graph = {
            key: set(value.get("premise_requirements") or []).union(
                item
                for group in (proof_contracts.get(key, {}).get("activation_requirement_groups") or [])
                for item in group
            )
            for key, value in requirements.items()
        }
        tuple(TopologicalSorter(graph).static_order())
    except CycleError as exc:
        raise ValueError("Cyclic requirement premises") from exc
    return pack


REQUIREMENT_PACK = load_requirement_pack()
REQUIREMENT_DEFINITIONS: dict[str, dict[str, Any]] = REQUIREMENT_PACK["requirements"]
REQUIREMENT_PROFILES: dict[str, list[dict[str, Any]]] = REQUIREMENT_PACK["profiles"]
UNCONFIGURED_POLICY_VALUES = frozenset(str(item) for item in REQUIREMENT_PACK.get("unconfigured_policy_values") or [])
PROOF_CONTRACT_VERSION = str(REQUIREMENT_PACK.get("contract_version") or REQUIREMENT_PACK["requirement_pack_version"])
PROOF_CONTRACT_DEFINITIONS: dict[str, dict[str, Any]] = REQUIREMENT_PACK.get("proof_contracts") or {}


def profile_requirements(profile_id: str, *, required: bool | None = None) -> tuple[str, ...]:
    rows = REQUIREMENT_PROFILES.get(str(profile_id or "").strip(), [])
    return tuple(
        str(row["id"])
        for row in rows
        if required is None or bool(row.get("required", True)) is required
    )


AP_THREE_WAY_REQUIREMENTS = profile_requirements("legacy_three_way")
AP_LITE_REQUIREMENTS = profile_requirements("ap_lite_po")

# Backward-compatible alias for existing AP review tests and stored cases.
CORE_REQUIREMENTS = AP_THREE_WAY_REQUIREMENTS

INVOICE_REQUIRED_FIELD_REQUIREMENTS = profile_requirements("invoice_only", required=True)
INVOICE_OPTIONAL_FIELD_REQUIREMENTS = profile_requirements("invoice_only", required=False)
INVOICE_FIELD_REQUIREMENTS = INVOICE_REQUIRED_FIELD_REQUIREMENTS + INVOICE_OPTIONAL_FIELD_REQUIREMENTS

DEFAULT_REQUIREMENT_LABELS = {
    requirement_id: str(definition.get("label") or requirement_id.replace("_", " "))
    for requirement_id, definition in REQUIREMENT_DEFINITIONS.items()
}
KNOWN_REQUIREMENTS = frozenset(REQUIREMENT_DEFINITIONS)
COMPILER_DERIVED_REQUIREMENTS = frozenset(
    requirement_id
    for requirement_id, definition in REQUIREMENT_DEFINITIONS.items()
    if definition.get("owner") == "compiler"
)
REVIEWER_DERIVED_REQUIREMENTS = frozenset(
    requirement_id
    for requirement_id, definition in REQUIREMENT_DEFINITIONS.items()
    if definition.get("owner") == "reviewer"
)
COMPILER_AUTHORITY_REQUIREMENTS = frozenset(KNOWN_REQUIREMENTS - {
    requirement_id
    for requirement_id, definition in REQUIREMENT_DEFINITIONS.items()
    if definition.get("owner") == "evidence"
})
AUTO_DERIVED_COMPILER_REQUIREMENTS = frozenset(
    requirement_id
    for requirement_id in COMPILER_AUTHORITY_REQUIREMENTS
    if (PROOF_CONTRACT_DEFINITIONS.get(requirement_id) or {}).get("activation", "derived" if requirement_id in COMPILER_DERIVED_REQUIREMENTS else "explicit") == "derived"
)
EXPLICIT_COMPILER_REQUIREMENTS = COMPILER_AUTHORITY_REQUIREMENTS - AUTO_DERIVED_COMPILER_REQUIREMENTS
DYNAMIC_SUPPORT_REQUIREMENTS = frozenset(str(item) for item in REQUIREMENT_PACK.get("dynamic_support_requirements") or [])
if not DYNAMIC_SUPPORT_REQUIREMENTS.issubset(KNOWN_REQUIREMENTS - COMPILER_AUTHORITY_REQUIREMENTS):
    raise ValueError("Invalid dynamic_support_requirements in requirement pack")


def requirement_definition(requirement_id: str) -> dict[str, Any] | None:
    return REQUIREMENT_DEFINITIONS.get(str(requirement_id or "").strip())


def requirement_label(requirement_id: str) -> str:
    value = str(requirement_id or "").strip()
    return DEFAULT_REQUIREMENT_LABELS.get(value, value.replace("_", " ").strip() or "requirement")


def requirement_kind(requirement_id: str) -> str:
    definition = requirement_definition(requirement_id) or {}
    return str(definition.get("kind") or "field")


def requirement_owner(requirement_id: str) -> str:
    definition = requirement_definition(requirement_id) or {}
    return str(definition.get("owner") or "evidence")


def requirement_premises(requirement_id: str) -> tuple[str, ...]:
    definition = requirement_definition(requirement_id) or {}
    return tuple(str(item) for item in definition.get("premise_requirements") or [])


def requirement_unconfigured_policy_values(requirement_id: str) -> tuple[str, ...]:
    definition = requirement_definition(requirement_id) or {}
    return tuple(
        str(item)
        for item in definition.get("required_policy_values") or []
        if str(item) in UNCONFIGURED_POLICY_VALUES
    )


def requirement_contract_definition(requirement_id: str) -> dict[str, Any]:
    return PROOF_CONTRACT_DEFINITIONS.get(str(requirement_id or "").strip(), {})


def requirement_proof_authority(requirement_id: str) -> str:
    return "evidence" if requirement_owner(requirement_id) == "evidence" else "compiler"


def requirement_activation(requirement_id: str) -> str:
    value = str(requirement_id or "").strip()
    contract = requirement_contract_definition(value)
    return str(contract.get("activation") or ("derived" if value in COMPILER_DERIVED_REQUIREMENTS else "explicit"))


def requirement_proof_template(requirement_id: str) -> str:
    contract = requirement_contract_definition(requirement_id)
    if contract:
        return str(contract["proof_template"])
    return "evidence_support" if requirement_proof_authority(requirement_id) == "evidence" else "semantic_gate"


def default_requirement_required(requirement_id: str) -> bool:
    definition = requirement_definition(requirement_id)
    return bool(definition.get("default_required", True)) if definition else True


def is_known_requirement(requirement_id: str) -> bool:
    return str(requirement_id or "").strip() in KNOWN_REQUIREMENTS
