from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from typing import Iterable

from app.compiler_runtime.graph_walk import reachable_ids
from app.compiler_runtime.models import ProofNode, ProofPlan
from app.domain.invoice_requirements import REQUIREMENT_PROOF_SIGNATURES
from app.proof_schema import ProofFacet, ProofSignature, ProofTerm, RootComposition


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
        reachable = reachable_ids(root_id, lambda node_id: nodes[node_id].depends_on)
        checks = [nodes[node_id] for node_id in reachable if nodes[node_id].kind == "CHECK"]

        allowed_roles_by_check: dict[str, set[str]] = {node.id: set() for node in checks}
        for node in checks:
            for declared_signature in self._signatures:
                if declared_signature.requirement_id not in node.requirement_refs:
                    continue
                for facet in declared_signature.facets:
                    if facet.id in node.facet_refs:
                        allowed_roles_by_check[node.id].update(facet.required_semantic_roles)
            unexpected = sorted(set(node.semantic_role_refs) - allowed_roles_by_check[node.id])
            if unexpected:
                raise ValueError(
                    f"CHECK {node.id!r} declares semantic roles outside its ProofSignature facets: "
                    f"{unexpected}"
                )

        for facet in signature.facets:
            facet_checks = [node for node in checks if facet.id in node.facet_refs]
            if not facet_checks:
                raise ValueError(
                    f"Required facet {facet.id!r} is not reachable from "
                    f"requirement root {signature.requirement_id!r}"
                )
            for node in facet_checks:
                missing_roles = sorted(
                    set(facet.required_semantic_roles) - set(node.semantic_role_refs)
                )
                if missing_roles:
                    raise ValueError(
                        f"Required facet {facet.id!r} CHECK {node.id!r} must declare every "
                        f"required semantic role: {missing_roles}"
                    )

            if "WITNESS" in facet.minimum_proof_terms:
                for node in facet_checks:
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
