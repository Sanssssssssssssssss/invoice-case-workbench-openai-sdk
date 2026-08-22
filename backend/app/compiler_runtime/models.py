from __future__ import annotations

import hashlib
import json
import re
from graphlib import CycleError, TopologicalSorter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


NodeKind = Literal["CHECK", "ALL", "ANY"]
AssessmentStatus = Literal["SUPPORTED", "CONTRADICTED", "NOT_FOUND"]
ExecutionStatus = Literal["COMPLETED", "PARTIAL", "FAILED"]
SemanticRole = Literal[
    "COMPONENT_OBSERVATION",
    "COMPONENT_APPLICABILITY",
    "COMPONENT_RECONCILIATION",
]
BusinessGapCode = Literal[
    "SOURCE_MISSING",
    "SOURCE_AMBIGUOUS",
    "BINDING_MISSING",
    "POLICY_UNCONFIGURED",
    "WITNESS_MISSING",
]


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require_text(value: str, *, field_name: str) -> str:
    result = value.strip()
    if not result:
        raise ValueError(f"{field_name} must not be empty")
    return result


def _unique_strings(value: list[str], *, field_name: str) -> list[str]:
    result = [_require_text(item, field_name=field_name) for item in value]
    if len(set(result)) != len(result):
        raise ValueError(f"{field_name} must not contain duplicates")
    return result


class _CompilerModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProofNode(_CompilerModel):
    id: str
    kind: NodeKind
    statement: str = ""
    depends_on: list[str] = Field(default_factory=list)
    upstream_check_ids: list[str] = Field(default_factory=list)
    requirement_refs: list[str] = Field(default_factory=list)
    policy_refs: list[str] = Field(default_factory=list)
    facet_refs: list[str] = Field(default_factory=list)
    semantic_role_refs: list[SemanticRole] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _require_text(value, field_name="node id")

    @field_validator(
        "depends_on",
        "upstream_check_ids",
        "requirement_refs",
        "policy_refs",
        "facet_refs",
        "semantic_role_refs",
    )
    @classmethod
    def validate_references(cls, value: list[str], info: Any) -> list[str]:
        return _unique_strings(value, field_name=info.field_name)

    @model_validator(mode="after")
    def validate_shape(self) -> ProofNode:
        self.statement = self.statement.strip()
        if self.kind == "CHECK":
            if not self.statement:
                raise ValueError(f"CHECK node {self.id!r} requires a statement")
            if self.depends_on:
                raise ValueError(f"CHECK node {self.id!r} cannot have status dependencies")
            if not self.requirement_refs:
                raise ValueError(f"CHECK node {self.id!r} requires at least one requirement ref")
            return self

        if self.statement:
            raise ValueError(f"{self.kind} node {self.id!r} cannot contain a check statement")
        if self.upstream_check_ids:
            raise ValueError(f"{self.kind} node {self.id!r} cannot consume upstream CHECK outputs")
        if self.requirement_refs or self.policy_refs or self.facet_refs or self.semantic_role_refs:
            raise ValueError(
                f"{self.kind} node {self.id!r} cannot contain requirement, policy, facet, or semantic role refs"
            )
        if not self.depends_on:
            raise ValueError(f"{self.kind} node {self.id!r} requires at least one dependency")
        return self


class ProofPlan(_CompilerModel):
    plan_id: str
    version: str = "1"
    objective: str
    active_requirement_ids: list[str]
    policy_refs: list[str] = Field(default_factory=list)
    roots: dict[str, str]
    nodes: list[ProofNode]

    @field_validator("plan_id", "version", "objective")
    @classmethod
    def validate_text(cls, value: str, info: Any) -> str:
        return _require_text(value, field_name=info.field_name)

    @field_validator("active_requirement_ids", "policy_refs")
    @classmethod
    def validate_declared_refs(cls, value: list[str], info: Any) -> list[str]:
        return _unique_strings(value, field_name=info.field_name)

    @model_validator(mode="after")
    def validate_graph(self) -> ProofPlan:
        if not self.active_requirement_ids:
            raise ValueError("ProofPlan requires at least one active requirement")
        if not self.nodes:
            raise ValueError("ProofPlan requires at least one node")

        node_ids = [node.id for node in self.nodes]
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("ProofPlan node ids must be unique")
        nodes = {node.id: node for node in self.nodes}

        declared_requirements = set(self.active_requirement_ids)
        root_requirements = set(self.roots)
        if root_requirements != declared_requirements:
            missing = sorted(declared_requirements - root_requirements)
            extra = sorted(root_requirements - declared_requirements)
            raise ValueError(f"ProofPlan roots must exactly cover active requirements; missing={missing}, extra={extra}")
        blank_root_keys = [key for key, value in self.roots.items() if not key.strip() or not value.strip()]
        if blank_root_keys:
            raise ValueError("ProofPlan root requirement ids and node ids must not be empty")
        unknown_roots = sorted(set(self.roots.values()) - set(nodes))
        if unknown_roots:
            raise ValueError(f"ProofPlan roots reference unknown nodes: {unknown_roots}")

        unknown_dependencies = sorted({item for node in self.nodes for item in node.depends_on if item not in nodes})
        if unknown_dependencies:
            raise ValueError(f"ProofPlan contains unknown dependencies: {unknown_dependencies}")
        unknown_upstream_checks = sorted(
            {
                upstream_id
                for node in self.nodes
                for upstream_id in node.upstream_check_ids
                if upstream_id not in nodes
            }
        )
        if unknown_upstream_checks:
            raise ValueError(
                f"ProofPlan references unknown upstream CHECKs: {unknown_upstream_checks}"
            )
        invalid_upstream_checks = sorted(
            (node.id, upstream_id)
            for node in self.nodes
            if node.kind == "CHECK"
            for upstream_id in node.upstream_check_ids
            if nodes[upstream_id].kind != "CHECK"
        )
        if invalid_upstream_checks:
            raise ValueError(
                "upstream_check_ids must reference CHECK nodes: "
                f"{invalid_upstream_checks}"
            )
        try:
            tuple(
                TopologicalSorter(
                    {
                        node.id: set(node.depends_on) | set(node.upstream_check_ids)
                        for node in self.nodes
                    }
                ).static_order()
            )
        except CycleError as exc:
            raise ValueError("ProofPlan must be acyclic") from exc

        referenced_policies = {
            policy for node in self.nodes if node.kind == "CHECK" for policy in node.policy_refs
        }
        if not self.policy_refs and referenced_policies:
            self.policy_refs = sorted(referenced_policies)
        declared_policies = set(self.policy_refs)
        for node in self.nodes:
            unknown_requirements = sorted(set(node.requirement_refs) - declared_requirements)
            if unknown_requirements:
                raise ValueError(f"CHECK node {node.id!r} references inactive requirements: {unknown_requirements}")
            unknown_policies = sorted(set(node.policy_refs) - declared_policies)
            if unknown_policies:
                raise ValueError(f"CHECK node {node.id!r} references undeclared policies: {unknown_policies}")

        reachable_by_requirement = {
            requirement_id: self._reachable_from(root_id, nodes)
            for requirement_id, root_id in self.roots.items()
        }
        reachable = set().union(*reachable_by_requirement.values())
        disconnected = sorted(set(nodes) - reachable)
        if disconnected:
            raise ValueError(f"ProofPlan contains nodes that do not lead to a requirement root: {disconnected}")

        for requirement_id, reachable_ids in reachable_by_requirement.items():
            covered = any(
                requirement_id in nodes[node_id].requirement_refs
                for node_id in reachable_ids
                if nodes[node_id].kind == "CHECK"
            )
            if not covered:
                raise ValueError(f"Requirement {requirement_id!r} is not covered by a CHECK below its root")

        for node in self.nodes:
            if node.kind != "CHECK":
                continue
            for requirement_id in node.requirement_refs:
                if node.id not in reachable_by_requirement[requirement_id]:
                    raise ValueError(
                        f"CHECK node {node.id!r} references requirement {requirement_id!r} "
                        "but is not reachable from that requirement root"
                    )

        covered_policies = {policy for node in self.nodes for policy in node.policy_refs}
        missing_policies = sorted(declared_policies - covered_policies)
        if missing_policies:
            raise ValueError(f"Declared policy refs are not covered by a CHECK: {missing_policies}")
        return self

    @staticmethod
    def _reachable_from(root_id: str, nodes: dict[str, ProofNode]) -> set[str]:
        result: set[str] = set()
        pending = [root_id]
        while pending:
            node_id = pending.pop()
            if node_id in result:
                continue
            result.add(node_id)
            pending.extend(nodes[node_id].depends_on)
        return result

    def content_hash(self) -> str:
        payload = self.model_dump(mode="json")
        payload["active_requirement_ids"] = sorted(payload["active_requirement_ids"])
        payload["policy_refs"] = sorted(payload["policy_refs"])
        payload["nodes"] = sorted(payload["nodes"], key=lambda item: item["id"])
        for node in payload["nodes"]:
            node["depends_on"] = sorted(node["depends_on"])
            node["upstream_check_ids"] = sorted(node["upstream_check_ids"])
            node["requirement_refs"] = sorted(node["requirement_refs"])
            node["policy_refs"] = sorted(node["policy_refs"])
            node["facet_refs"] = sorted(node["facet_refs"])
            node["semantic_role_refs"] = sorted(node["semantic_role_refs"])
        return _stable_hash(payload)


class Claim(_CompilerModel):
    id: str
    subject: str
    predicate: str
    value: Any
    source_id: str
    quote: str
    locator: str
    confidence: Literal["low", "medium", "high"] = "medium"
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id", "subject", "predicate", "source_id", "quote", "locator")
    @classmethod
    def validate_text(cls, value: str, info: Any) -> str:
        return _require_text(value, field_name=info.field_name)

    @field_validator("attributes")
    @classmethod
    def validate_observation_attributes(cls, value: dict[str, Any]) -> dict[str, Any]:
        # Claim is the source-observation layer. Cross-claim meaning belongs in a
        # SemanticBindingProposal and arithmetic lineage belongs in a Witness.
        reserved = {
            "binding",
            "binding_group",
            "binding_id",
            "claim_ids",
            "operands",
            "related_claim_ids",
            "relation",
            "term_refs",
            "witness_ids",
        }
        forbidden = sorted(reserved.intersection(value))
        if forbidden:
            raise ValueError(
                "Claim attributes cannot encode semantic bindings or calculations: "
                f"{forbidden}"
            )
        return value


class EvidenceIR(_CompilerModel):
    schema_version: str = "1"
    source_ids: list[str] = Field(default_factory=list)
    source_fingerprints: dict[str, str] = Field(default_factory=dict)
    claims: list[Claim] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        return _require_text(value, field_name="schema_version")

    @field_validator("source_ids")
    @classmethod
    def validate_source_ids(cls, value: list[str]) -> list[str]:
        return _unique_strings(value, field_name="source_ids")

    @model_validator(mode="after")
    def validate_claims(self) -> EvidenceIR:
        normalized_fingerprints = {
            _require_text(source_id, field_name="source_fingerprint source id"):
            _require_text(fingerprint, field_name="source fingerprint")
            for source_id, fingerprint in self.source_fingerprints.items()
        }
        self.source_fingerprints = normalized_fingerprints
        if normalized_fingerprints and set(normalized_fingerprints) != set(self.source_ids):
            missing = sorted(set(self.source_ids) - set(normalized_fingerprints))
            extra = sorted(set(normalized_fingerprints) - set(self.source_ids))
            raise ValueError(
                "EvidenceIR source fingerprints must cover every source when supplied; "
                f"missing={missing}, extra={extra}"
            )
        claim_ids = [claim.id for claim in self.claims]
        if len(set(claim_ids)) != len(claim_ids):
            raise ValueError("EvidenceIR claim ids must be unique")
        unknown_sources = sorted({claim.source_id for claim in self.claims} - set(self.source_ids))
        if unknown_sources:
            raise ValueError(f"EvidenceIR claims reference unknown sources: {unknown_sources}")
        return self

    def content_hash(self) -> str:
        payload = self.model_dump(mode="json")
        payload["source_ids"] = sorted(payload["source_ids"])
        payload["source_fingerprints"] = dict(sorted(payload["source_fingerprints"].items()))
        payload["claims"] = sorted(payload["claims"], key=lambda item: item["id"])
        return _stable_hash(payload)

    def source_snapshot_hash(self) -> str:
        """Hash only the admitted, immutable source snapshot used by Witnesses."""
        return _stable_hash(
            {
                "kind": "compiler_runtime.evidence_source_snapshot",
                "schema_version": self.schema_version,
                "source_ids": sorted(self.source_ids),
                "source_fingerprints": dict(sorted(self.source_fingerprints.items())),
            }
        )


class StrongStatusLink(_CompilerModel):
    """Verifier-owned polarity link to one replayable boolean Witness.

    The link intentionally carries no result, threshold, formula, or Policy
    value.  The Proof Kernel obtains the boolean only by replaying the named
    Witness and derives the false polarity as the opposite strong status.
    """

    witness_id: str
    true_status: Literal["SUPPORTED", "CONTRADICTED"]

    @field_validator("witness_id")
    @classmethod
    def validate_witness_id(cls, value: str) -> str:
        return _require_text(value, field_name="witness_id")


class CheckAssessment(_CompilerModel):
    check_id: str
    claim_ids: list[str] = Field(default_factory=list)
    accepted_binding_ids: list[str] = Field(default_factory=list)
    accepted_witness_ids: list[str] = Field(default_factory=list)
    strong_status_links: list["StrongStatusLink"] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    examined_source_ids: list[str] = Field(default_factory=list)
    reason: str = ""
    missing_fact: str = ""
    gap_code: BusinessGapCode | None = None
    # Keep the verdict last in the structured output. Autoregressive models must
    # finish checking the evidence before committing to the classification.
    status: AssessmentStatus

    @field_validator("check_id")
    @classmethod
    def validate_check_id(cls, value: str) -> str:
        return _require_text(value, field_name="check_id")

    @field_validator(
        "claim_ids",
        "accepted_binding_ids",
        "accepted_witness_ids",
        "source_ids",
        "examined_source_ids",
    )
    @classmethod
    def validate_refs(cls, value: list[str], info: Any) -> list[str]:
        return _unique_strings(value, field_name=info.field_name)

    @model_validator(mode="after")
    def normalize_explanation(self) -> CheckAssessment:
        self.reason = self.reason.strip()
        self.missing_fact = self.missing_fact.strip()
        witness_ids = [item.witness_id for item in self.strong_status_links]
        if len(set(witness_ids)) != len(witness_ids):
            raise ValueError("strong_status_links must not contain duplicate witness ids")
        return self


_EXPLICIT_FINAL_STATUS = re.compile(
    r"\b(?:final\s+(?:classification|status)|verdict)\s*(?:is|:|=|should\s+be|must\s+be)?\s*"
    r"`?(SUPPORTED|CONTRADICTED|NOT_FOUND)`?\b"
    r"|\b(?:the\s+)?check\s+(?:is|should\s+be|must\s+be|is\s+classified\s+as)\s*"
    r"`?(SUPPORTED|CONTRADICTED|NOT_FOUND)`?\b",
    flags=re.IGNORECASE,
)


def explicit_final_statuses(reason: str) -> set[str]:
    """Return only statuses the verifier explicitly presents as its conclusion."""
    return {
        next(value for value in match if value).upper()
        for match in _EXPLICIT_FINAL_STATUS.findall(reason)
    }


class ReviewArtifact(_CompilerModel):
    plan: ProofPlan
    plan_hash: str
    proof_signature_hash: str = ""
    evidence_ir: EvidenceIR
    evidence_snapshot_hash: str
    assessments: list[CheckAssessment] = Field(default_factory=list)
    binding_proposals: list["SemanticBindingProposal"] = Field(default_factory=list)
    calculation_witnesses: list["CalculationWitness"] = Field(default_factory=list)
    submitted_claim_refs: dict[str, list[str]] = Field(default_factory=dict)
    submitted_binding_refs: dict[str, list[str]] = Field(default_factory=dict)
    submitted_witness_refs: dict[str, list[str]] = Field(default_factory=dict)
    policy_hash: str
    resolved_policy_terms: dict[str, Any] = Field(default_factory=dict)
    unconfigured_policy_refs: list[str] = Field(default_factory=list)
    execution_status: ExecutionStatus = "COMPLETED"
    compiler_version: str
    model: str
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    artifact_hash: str = ""

    @field_validator(
        "plan_hash",
        "evidence_snapshot_hash",
        "policy_hash",
        "compiler_version",
        "model",
    )
    @classmethod
    def validate_text(cls, value: str, info: Any) -> str:
        return _require_text(value, field_name=info.field_name)

    @field_validator("proof_signature_hash")
    @classmethod
    def normalize_proof_signature_hash(cls, value: str) -> str:
        return value.strip()

    @field_validator("unconfigured_policy_refs")
    @classmethod
    def validate_unconfigured_policy_refs(cls, value: list[str]) -> list[str]:
        return _unique_strings(value, field_name="unconfigured_policy_refs")

    @field_validator("submitted_claim_refs", "submitted_binding_refs", "submitted_witness_refs")
    @classmethod
    def validate_submitted_refs(
        cls,
        value: dict[str, list[str]],
        info: Any,
    ) -> dict[str, list[str]]:
        return {
            _require_text(check_id, field_name=f"{info.field_name} check id"): _unique_strings(
                ref_ids,
                field_name=f"{info.field_name}[{check_id!r}]",
            )
            for check_id, ref_ids in value.items()
        }

    @field_validator("artifact_hash")
    @classmethod
    def normalize_artifact_hash(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_assessment_ids(self) -> ReviewArtifact:
        assessment_ids = [assessment.check_id for assessment in self.assessments]
        if len(set(assessment_ids)) != len(assessment_ids):
            raise ValueError("ReviewArtifact contains duplicate assessments for a check")
        binding_ids = [item.id for item in self.binding_proposals]
        witness_ids = [item.id for item in self.calculation_witnesses]
        if len(set(binding_ids)) != len(binding_ids):
            raise ValueError("ReviewArtifact contains duplicate semantic binding ids")
        if len(set(witness_ids)) != len(witness_ids):
            raise ValueError("ReviewArtifact contains duplicate calculation witness ids")
        # Cross-object truth (whether a ref exists, belongs to this CHECK/facet,
        # or was actually submitted) is deliberately not a Pydantic concern.
        # The Runtime rejects it on the normal path and the Proof Kernel must
        # diagnose hostile/stale artifacts fail-closed instead of losing that
        # attack surface during schema parsing.
        return self

    def content_hash(self) -> str:
        payload = self.model_dump(mode="json", exclude={"artifact_hash"})
        payload["assessments"] = sorted(payload["assessments"], key=lambda item: item["check_id"])
        for assessment in payload["assessments"]:
            assessment["strong_status_links"] = sorted(
                assessment["strong_status_links"],
                key=lambda item: item["witness_id"],
            )
        payload["binding_proposals"] = sorted(
            payload["binding_proposals"], key=lambda item: item["id"]
        )
        payload["calculation_witnesses"] = sorted(
            payload["calculation_witnesses"], key=lambda item: item["id"]
        )
        for field_name in (
            "submitted_claim_refs",
            "submitted_binding_refs",
            "submitted_witness_refs",
        ):
            payload[field_name] = {
                check_id: sorted(ref_ids)
                for check_id, ref_ids in sorted(payload[field_name].items())
            }
        payload["unconfigured_policy_refs"] = sorted(payload["unconfigured_policy_refs"])
        payload["prompt_versions"] = dict(sorted(payload["prompt_versions"].items()))
        return _stable_hash(payload)


class NodeResult(_CompilerModel):
    node_id: str
    kind: NodeKind
    status: AssessmentStatus
    reason: str = ""
    claim_ids: list[str] = Field(default_factory=list)
    binding_ids: list[str] = Field(default_factory=list)
    witness_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    gap_code: BusinessGapCode | None = None

    @field_validator("claim_ids", "binding_ids", "witness_ids", "source_ids")
    @classmethod
    def validate_result_refs(cls, value: list[str], info: Any) -> list[str]:
        return _unique_strings(value, field_name=info.field_name)


class CompilationDiagnostic(_CompilerModel):
    code: str
    message: str
    node_id: str = ""
    requirement_id: str = ""
    blocking: bool = True


class ProofObligation(_CompilerModel):
    id: str
    requirement_id: str
    check_id: str
    missing_fact: str
    blocking: bool = True
    candidate_actions: list[str] = Field(
        default_factory=lambda: ["list_sources", "read_source", "bind_claim", "submit_check"]
    )


class DecisionProof(_CompilerModel):
    requirement_id: str
    root_node_id: str
    status: AssessmentStatus
    supporting_check_ids: list[str] = Field(default_factory=list)
    contradicting_check_ids: list[str] = Field(default_factory=list)
    unresolved_check_ids: list[str] = Field(default_factory=list)
    obligation_ids: list[str] = Field(default_factory=list)
    plan_hash: str
    evidence_snapshot_hash: str
    policy_hash: str
    stop_reason: str


class CompiledProof(_CompilerModel):
    node_results: list[NodeResult] = Field(default_factory=list)
    decisions: list[DecisionProof] = Field(default_factory=list)
    obligations: list[ProofObligation] = Field(default_factory=list)
    diagnostics: list[CompilationDiagnostic] = Field(default_factory=list)

    def decision_for(self, requirement_id: str) -> DecisionProof | None:
        return next((item for item in self.decisions if item.requirement_id == requirement_id), None)


# Resolve typed proof-term fields without making proof_terms depend on a partially
# initialized ReviewArtifact module. proof_terms imports Claim, which is defined
# above before this late import runs.
from .proof_terms import CalculationWitness, SemanticBindingProposal  # noqa: E402

ReviewArtifact.model_rebuild(
    _types_namespace={
        "CalculationWitness": CalculationWitness,
        "SemanticBindingProposal": SemanticBindingProposal,
    }
)
