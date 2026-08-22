from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from typing import Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.compiler_runtime.models import ProofNode, ProofPlan, SemanticRole
from app.domain.invoice_requirements import REQUIREMENT_PROOF_SIGNATURES


ProofTerm = Literal["CLAIM", "BINDING", "WITNESS"]
RootComposition = Literal["ALL_REQUIRED", "ANY_SUFFICIENT"]


def _non_empty(value: str, field_name: str) -> str:
    result = value.strip()
    if not result:
        raise ValueError(f"{field_name} must not be empty")
    return result


class _SignatureModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProofFacet(_SignatureModel):
    id: str
    minimum_proof_terms: list[ProofTerm]
    semantic_contract: str = ""
    required_semantic_roles: list[SemanticRole] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _non_empty(value, "facet id")

    @field_validator("minimum_proof_terms")
    @classmethod
    def validate_terms(cls, value: list[ProofTerm]) -> list[ProofTerm]:
        if not value:
            raise ValueError("minimum_proof_terms must not be empty")
        if len(set(value)) != len(value):
            raise ValueError("minimum_proof_terms must not contain duplicates")
        return value

    @field_validator("semantic_contract")
    @classmethod
    def validate_semantic_contract(cls, value: str) -> str:
        return value.strip()

    @field_validator("required_semantic_roles")
    @classmethod
    def validate_semantic_roles(cls, value: list[SemanticRole]) -> list[SemanticRole]:
        if len(set(value)) != len(value):
            raise ValueError("required_semantic_roles must not contain duplicates")
        return value

    @model_validator(mode="after")
    def validate_semantic_shape(self) -> ProofFacet:
        if self.required_semantic_roles and not self.semantic_contract:
            raise ValueError("required_semantic_roles require a semantic_contract")
        return self


class ProofSignature(_SignatureModel):
    signature_id: str
    version: str
    requirement_id: str
    root_composition: RootComposition
    required_policy_refs: list[str] = Field(default_factory=list)
    facets: list[ProofFacet]

    @field_validator("signature_id", "version", "requirement_id")
    @classmethod
    def validate_text(cls, value: str, info) -> str:
        return _non_empty(value, info.field_name)

    @field_validator("required_policy_refs")
    @classmethod
    def validate_policy_refs(cls, value: list[str]) -> list[str]:
        result = [_non_empty(item, "required_policy_refs") for item in value]
        if len(set(result)) != len(result):
            raise ValueError("required_policy_refs must not contain duplicates")
        return result

    @model_validator(mode="after")
    def validate_facets(self) -> ProofSignature:
        if not self.facets:
            raise ValueError("facets must not be empty")
        facet_ids = [facet.id for facet in self.facets]
        if len(set(facet_ids)) != len(facet_ids):
            raise ValueError("facet ids must be unique")
        return self


@lru_cache(maxsize=1)
def proof_signatures() -> tuple[ProofSignature, ...]:
    return tuple(ProofSignature.model_validate(item) for item in REQUIREMENT_PROOF_SIGNATURES)


def proof_signature_for(requirement_id: str) -> ProofSignature | None:
    normalized = str(requirement_id or "").strip()
    return next(
        (signature for signature in proof_signatures() if signature.requirement_id == normalized),
        None,
    )


def proof_signature_hash_for(requirement_ids: Iterable[str]) -> str:
    """Hash the active minimal type constraints, including the empty set."""

    active_ids = {str(item or "").strip() for item in requirement_ids}
    payload = [
        signature.model_dump(mode="json")
        for signature in sorted(proof_signatures(), key=lambda item: item.requirement_id)
        if signature.requirement_id in active_ids
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class PlanConformanceGate:
    """Type-check a freely generated ProofPlan against minimal proof signatures."""

    def __init__(self, signatures: Iterable[ProofSignature] | None = None) -> None:
        selected = tuple(signatures) if signatures is not None else proof_signatures()
        requirement_ids = [signature.requirement_id for signature in selected]
        if len(set(requirement_ids)) != len(requirement_ids):
            raise ValueError("PlanConformanceGate requires at most one signature per requirement")
        self._signatures = selected

    def validate(self, plan: ProofPlan) -> None:
        nodes = {node.id: node for node in plan.nodes}
        for signature in self._signatures:
            if signature.requirement_id not in plan.active_requirement_ids:
                continue
            self._validate_signature(plan, nodes, signature)

    def _validate_signature(
        self,
        plan: ProofPlan,
        nodes: dict[str, ProofNode],
        signature: ProofSignature,
    ) -> None:
        root_id = plan.roots.get(signature.requirement_id)
        if not root_id or root_id not in nodes:
            raise ValueError(
                f"ProofPlan has no root for signature requirement {signature.requirement_id!r}"
            )
        reachable = self._reachable(root_id, nodes)
        checks = [nodes[node_id] for node_id in reachable if nodes[node_id].kind == "CHECK"]

        allowed_roles_by_check: dict[str, set[str]] = {node.id: set() for node in checks}
        for node in checks:
            for declared_signature in self._signatures:
                if declared_signature.requirement_id not in node.requirement_refs:
                    continue
                for facet in declared_signature.facets:
                    if facet.id in node.facet_refs:
                        allowed_roles_by_check[node.id].update(facet.required_semantic_roles)
                        missing_roles = sorted(
                            set(facet.required_semantic_roles) - set(node.semantic_role_refs)
                        )
                        if missing_roles:
                            raise ValueError(
                                f"CHECK {node.id!r} must declare every required semantic role "
                                f"for facet {facet.id!r}: {missing_roles}"
                            )
            unexpected = sorted(set(node.semantic_role_refs) - allowed_roles_by_check[node.id])
            if unexpected:
                raise ValueError(
                    f"CHECK {node.id!r} declares semantic roles outside its ProofSignature facets: "
                    f"{unexpected}"
                )

        for facet in signature.facets:
            if not any(facet.id in node.facet_refs for node in checks):
                raise ValueError(
                    f"Required facet {facet.id!r} is not reachable from "
                    f"requirement root {signature.requirement_id!r}"
                )

            if "WITNESS" in facet.minimum_proof_terms:
                for node in checks:
                    if facet.id not in node.facet_refs:
                        continue
                    missing = sorted(
                        set(signature.required_policy_refs) - set(node.policy_refs)
                    )
                    if missing:
                        raise ValueError(
                            f"WITNESS facet {facet.id!r} CHECK {node.id!r} must declare "
                            f"required policy refs: {missing}"
                        )

        for policy_ref in signature.required_policy_refs:
            if policy_ref not in plan.policy_refs or not any(
                policy_ref in node.policy_refs for node in checks
            ):
                raise ValueError(
                    f"Required policy ref {policy_ref!r} is not covered below "
                    f"requirement root {signature.requirement_id!r}"
                )

        facet_ids = frozenset(facet.id for facet in signature.facets)
        if signature.root_composition == "ALL_REQUIRED":
            for facet_id in facet_ids:
                if self._can_succeed(
                    root_id,
                    nodes,
                    check_value=lambda node, current=facet_id: current not in node.facet_refs,
                ):
                    raise ValueError(
                        f"ALL_REQUIRED root can bypass required facet {facet_id!r}"
                    )
        else:
            if self._can_succeed(
                root_id,
                nodes,
                check_value=lambda node: not facet_ids.intersection(node.facet_refs),
            ):
                raise ValueError("ANY_SUFFICIENT root can bypass every declared facet")
            for facet_id in facet_ids:
                if not self._can_succeed(
                    root_id,
                    nodes,
                    check_value=lambda node, current=facet_id: (
                        current in node.facet_refs
                        or not facet_ids.intersection(node.facet_refs)
                    ),
                ):
                    raise ValueError(
                        f"ANY_SUFFICIENT root does not admit facet {facet_id!r} as a sufficient path"
                    )

        for policy_ref in signature.required_policy_refs:
            if self._can_succeed(
                root_id,
                nodes,
                check_value=lambda node, current=policy_ref: current not in node.policy_refs,
            ):
                raise ValueError(
                    f"Requirement root can bypass required policy ref {policy_ref!r}"
                )

    @staticmethod
    def _reachable(root_id: str, nodes: dict[str, ProofNode]) -> set[str]:
        reachable: set[str] = set()
        pending = [root_id]
        while pending:
            node_id = pending.pop()
            if node_id in reachable:
                continue
            reachable.add(node_id)
            pending.extend(nodes[node_id].depends_on)
        return reachable

    @classmethod
    def _can_succeed(
        cls,
        node_id: str,
        nodes: dict[str, ProofNode],
        *,
        check_value,
    ) -> bool:
        node = nodes[node_id]
        if node.kind == "CHECK":
            return bool(check_value(node))
        child_values = [
            cls._can_succeed(child_id, nodes, check_value=check_value)
            for child_id in node.depends_on
        ]
        return all(child_values) if node.kind == "ALL" else any(child_values)


def validate_plan_conformance(
    plan: ProofPlan,
    signatures: Iterable[ProofSignature] | None = None,
) -> None:
    PlanConformanceGate(signatures).validate(plan)


__all__ = [
    "PlanConformanceGate",
    "ProofFacet",
    "ProofSignature",
    "ProofTerm",
    "RootComposition",
    "proof_signature_for",
    "proof_signature_hash_for",
    "proof_signatures",
    "validate_plan_conformance",
]
