from __future__ import annotations

import json
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
POLICY_PATH = PROJECT_ROOT / "policies" / "aurora_ap_policy_v1.json"
_KINDS = {"document", "field", "cross_check", "visual", "risk_check"}
_OWNERS = {"evidence", "reviewer", "compiler"}
_HINT_ACTIVATIONS = {"explicit", "derived"}
_HINT_FIELDS = {"activation", "activation_requirement_groups", "capability", "target_predicate"}


def load_requirement_pack(path: Path = POLICY_PATH) -> dict[str, Any]:
    pack = json.loads(path.read_text(encoding="utf-8"))
    requirements = pack.get("requirements")
    profiles = pack.get("profiles")
    if not isinstance(requirements, dict) or not isinstance(profiles, dict):
        raise ValueError(f"Invalid requirement pack: {path}")
    if not str(pack.get("requirement_pack_version") or "").strip():
        raise ValueError("Invalid requirement pack version")
    planning_hints = pack.get("planning_hints") or {}
    if not isinstance(planning_hints, dict):
        raise ValueError("Invalid planning hints")
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
        profile_ids = {str(row["id"]) for row in rows}
        if "invoice" in profile_ids and "invoice_calculation_valid" not in profile_ids:
            raise ValueError(f"Invoice review profile must include invoice_calculation_valid: {profile_id}")
    for requirement_id, hint in planning_hints.items():
        if requirement_id not in requirements or not isinstance(hint, dict) or set(hint) - _HINT_FIELDS:
            raise ValueError(f"Invalid planning hint: {requirement_id}")
        if hint.get("activation", "explicit") not in _HINT_ACTIVATIONS:
            raise ValueError(f"Invalid planning hint activation: {requirement_id}")
        owner = requirements[requirement_id].get("owner")
        expected_activation = "derived" if owner == "compiler" else "explicit"
        if hint.get("activation", "explicit") != expected_activation:
            raise ValueError(f"Invalid planning hint authority: {requirement_id}")
        if not str(hint.get("capability") or "").strip() or not str(hint.get("target_predicate") or "").strip():
            raise ValueError(f"Invalid planning hint target: {requirement_id}")
        groups = hint.get("activation_requirement_groups") or []
        if not isinstance(groups, list) or any(
            not isinstance(group, list)
            or not group
            or any(str(item) not in requirements or str(item) == requirement_id for item in group)
            for group in groups
        ):
            raise ValueError(f"Invalid planning hint activation requirements: {requirement_id}")
        if expected_activation == "derived" and not groups:
            raise ValueError(f"Derived planning hint has no activation requirements: {requirement_id}")
    missing_hints = {
        requirement_id
        for requirement_id, definition in requirements.items()
        if definition.get("owner") != "evidence" and requirement_id not in planning_hints
    }
    if missing_hints:
        raise ValueError(f"Missing planning hints: {', '.join(sorted(missing_hints))}")
    try:
        graph = {
            key: set(value.get("premise_requirements") or []).union(
                item
                for group in (planning_hints.get(key, {}).get("activation_requirement_groups") or [])
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
REQUIREMENT_CATALOG_VERSION = str(REQUIREMENT_PACK["requirement_pack_version"])
REQUIREMENT_PLANNING_HINTS: dict[str, dict[str, Any]] = REQUIREMENT_PACK.get("planning_hints") or {}


def profile_requirements(profile_id: str, *, required: bool | None = None) -> tuple[str, ...]:
    rows = REQUIREMENT_PROFILES.get(str(profile_id or "").strip(), [])
    return tuple(
        str(row["id"])
        for row in rows
        if required is None or bool(row.get("required", True)) is required
    )


def _evidence_profile_requirements(profile_id: str) -> tuple[str, ...]:
    return tuple(
        requirement_id
        for requirement_id in profile_requirements(profile_id)
        if REQUIREMENT_DEFINITIONS[requirement_id].get("owner") == "evidence"
    )


AP_THREE_WAY_REQUIREMENTS = _evidence_profile_requirements("legacy_three_way")
AP_LITE_REQUIREMENTS = _evidence_profile_requirements("ap_lite_po")

# Backward-compatible alias for existing AP review tests and stored cases.
CORE_REQUIREMENTS = AP_THREE_WAY_REQUIREMENTS

def _invoice_material_requirements(*, required: bool) -> tuple[str, ...]:
    return tuple(
        requirement_id
        for requirement_id in profile_requirements("invoice_only", required=required)
        if REQUIREMENT_DEFINITIONS[requirement_id].get("owner") == "evidence"
        and REQUIREMENT_DEFINITIONS[requirement_id].get("kind") in {"field", "visual", "risk_check"}
    )


INVOICE_REQUIRED_FIELD_REQUIREMENTS = _invoice_material_requirements(required=True)
INVOICE_OPTIONAL_FIELD_REQUIREMENTS = _invoice_material_requirements(required=False)
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
    if (REQUIREMENT_PLANNING_HINTS.get(requirement_id) or {}).get(
        "activation", "derived" if requirement_id in COMPILER_DERIVED_REQUIREMENTS else "explicit"
    ) == "derived"
)
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


def requirement_evidence_type(requirement_id: str) -> str:
    """Return the policy-declared stored evidence type for a source requirement."""

    definition = requirement_definition(requirement_id) or {}
    return str(definition.get("evidence_type") or "").strip()


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


def default_requirement_required(requirement_id: str) -> bool:
    definition = requirement_definition(requirement_id)
    return bool(definition.get("default_required", True)) if definition else True


def is_known_requirement(requirement_id: str) -> bool:
    return str(requirement_id or "").strip() in KNOWN_REQUIREMENTS
