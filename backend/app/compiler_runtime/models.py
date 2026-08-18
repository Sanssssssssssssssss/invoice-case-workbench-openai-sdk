from __future__ import annotations

import hashlib
import json
from graphlib import CycleError, TopologicalSorter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


NodeKind = Literal["CHECK", "ALL", "ANY", "NOT"]
AssessmentStatus = Literal["SUPPORTED", "CONTRADICTED", "NOT_FOUND"]


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
    requirement_refs: list[str] = Field(default_factory=list)
    policy_refs: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _require_text(value, field_name="node id")

    @field_validator("depends_on", "requirement_refs", "policy_refs")
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
                raise ValueError(f"CHECK node {self.id!r} cannot have dependencies")
            if not self.requirement_refs:
                raise ValueError(f"CHECK node {self.id!r} requires at least one requirement ref")
            return self

        if self.statement:
            raise ValueError(f"{self.kind} node {self.id!r} cannot contain a check statement")
        if self.requirement_refs or self.policy_refs:
            raise ValueError(f"{self.kind} node {self.id!r} cannot contain requirement or policy refs")
        if self.kind == "NOT" and len(self.depends_on) != 1:
            raise ValueError(f"NOT node {self.id!r} requires exactly one dependency")
        if self.kind in {"ALL", "ANY"} and not self.depends_on:
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
        try:
            tuple(TopologicalSorter({node.id: set(node.depends_on) for node in self.nodes}).static_order())
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
            node["requirement_refs"] = sorted(node["requirement_refs"])
            node["policy_refs"] = sorted(node["policy_refs"])
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


class CheckAssessment(_CompilerModel):
    check_id: str
    status: AssessmentStatus
    claim_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    examined_source_ids: list[str] = Field(default_factory=list)
    reason: str = ""
    missing_fact: str = ""

    @field_validator("check_id")
    @classmethod
    def validate_check_id(cls, value: str) -> str:
        return _require_text(value, field_name="check_id")

    @field_validator("claim_ids", "source_ids", "examined_source_ids")
    @classmethod
    def validate_refs(cls, value: list[str], info: Any) -> list[str]:
        return _unique_strings(value, field_name=info.field_name)

    @model_validator(mode="after")
    def normalize_explanation(self) -> CheckAssessment:
        self.reason = self.reason.strip()
        self.missing_fact = self.missing_fact.strip()
        return self


class ReviewArtifact(_CompilerModel):
    plan: ProofPlan
    plan_hash: str
    evidence_ir: EvidenceIR
    evidence_snapshot_hash: str
    assessments: list[CheckAssessment] = Field(default_factory=list)
    submitted_claim_refs: dict[str, list[str]] = Field(default_factory=dict)
    policy_hash: str
    unconfigured_policy_refs: list[str] = Field(default_factory=list)
    compiler_version: str
    model: str
    prompt_versions: dict[str, str] = Field(default_factory=dict)

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

    @field_validator("unconfigured_policy_refs")
    @classmethod
    def validate_unconfigured_policy_refs(cls, value: list[str]) -> list[str]:
        return _unique_strings(value, field_name="unconfigured_policy_refs")

    @field_validator("submitted_claim_refs")
    @classmethod
    def validate_submitted_claim_refs(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        return {
            _require_text(check_id, field_name="submitted_claim_refs check id"): _unique_strings(
                claim_ids,
                field_name=f"submitted_claim_refs[{check_id!r}]",
            )
            for check_id, claim_ids in value.items()
        }

    @model_validator(mode="after")
    def validate_assessment_ids(self) -> ReviewArtifact:
        assessment_ids = [assessment.check_id for assessment in self.assessments]
        if len(set(assessment_ids)) != len(assessment_ids):
            raise ValueError("ReviewArtifact contains duplicate assessments for a check")
        check_ids = {node.id for node in self.plan.nodes if node.kind == "CHECK"}
        unknown_submissions = sorted(set(self.submitted_claim_refs) - check_ids)
        if unknown_submissions:
            raise ValueError(
                "ReviewArtifact contains submissions outside its ProofPlan CHECK nodes: "
                f"{unknown_submissions}"
            )
        unknown_policy_refs = sorted(set(self.unconfigured_policy_refs) - set(self.plan.policy_refs))
        if unknown_policy_refs:
            raise ValueError(
                "ReviewArtifact contains unconfigured policy refs outside its ProofPlan: "
                f"{unknown_policy_refs}"
            )
        return self


class NodeResult(_CompilerModel):
    node_id: str
    kind: NodeKind
    status: AssessmentStatus
    reason: str = ""
    claim_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)


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
