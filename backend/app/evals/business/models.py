from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvalAttachment(StrictModel):
    id: str
    path: str
    sha256: str
    content_type: str
    source_url: str
    source_revision: str
    license: str

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        normalized = value.lower()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise ValueError("sha256 must be a 64-character hexadecimal digest")
        return normalized


class BusinessEvalCase(StrictModel):
    schema_version: Literal["1"] = "1"
    case_id: str
    case_version: str
    title: str
    suite: Literal["atomic_dev", "composite_dev", "composite_holdout"]
    business_holes: list[str] = Field(min_length=1)
    user_message: str = Field(min_length=1)
    policy_version: str
    report_required: bool = True
    auto_approve_tools: list[Literal["write_case_file", "render_pdf"]] = Field(default_factory=list)
    attachments: list[EvalAttachment] = Field(min_length=1)

    @field_validator("case_id")
    @classmethod
    def validate_case_id(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
            raise ValueError("case_id must contain only letters, numbers, '_' or '-'")
        return value

    @model_validator(mode="after")
    def validate_report_approvals(self) -> "BusinessEvalCase":
        if self.report_required and set(self.auto_approve_tools) != {"write_case_file", "render_pdf"}:
            raise ValueError("report evals must approve exactly write_case_file and render_pdf")
        return self


class MeaningOracle(StrictModel):
    all_of: list[list[str]] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_meanings(self) -> "MeaningOracle":
        normalized: list[list[str]] = []
        for group in self.all_of:
            options = [item.strip() for item in group if item.strip()]
            if not options:
                raise ValueError("meaning groups must contain at least one non-empty option")
            if len(set(options)) != len(options):
                raise ValueError("meaning groups must not contain duplicate options")
            normalized.append(options)
        self.all_of = normalized
        return self


class ExpectedFact(StrictModel):
    id: str
    origin: Literal["source", "derived", "policy"]
    kind: Literal["decimal", "text"]
    value: str
    currency: str = ""
    tolerance: Decimal = Field(default=Decimal("0"), ge=0)
    source_role: str = ""
    source_quote: str = ""
    policy_ref: str = ""
    predicate_options: list[str] = Field(default_factory=list)
    required_in: list[Literal["evidence", "reasoning", "reply", "report"]] = Field(default_factory=list)

    @field_validator("predicate_options")
    @classmethod
    def validate_predicate_options(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if item.strip()]
        if len(normalized) != len(value) or len(set(normalized)) != len(normalized):
            raise ValueError("predicate_options must be non-empty and unique")
        return normalized

    @model_validator(mode="after")
    def validate_origin(self) -> "ExpectedFact":
        self.source_role = self.source_role.strip()
        self.source_quote = self.source_quote.strip()
        self.policy_ref = self.policy_ref.strip()
        if self.origin == "source":
            if not self.source_role or not self.source_quote:
                raise ValueError("source facts require source_role and source_quote")
            if self.policy_ref:
                raise ValueError("source facts cannot declare policy_ref")
        elif self.origin == "policy":
            if not self.policy_ref:
                raise ValueError("policy facts require policy_ref")
            if self.source_role or self.source_quote:
                raise ValueError("policy facts cannot declare source_role or source_quote")
        elif self.source_role or self.source_quote or self.policy_ref:
            raise ValueError("derived facts cannot declare source or policy bindings")
        return self


class TaskIntentOracle(StrictModel):
    objective: MeaningOracle
    required_milestone_ids: list[str] = Field(min_length=1)

    @field_validator("required_milestone_ids")
    @classmethod
    def validate_milestone_ids(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if item.strip()]
        if len(normalized) != len(value) or len(set(normalized)) != len(normalized):
            raise ValueError("required_milestone_ids must be non-empty and unique")
        return normalized


class SemanticMilestoneOracle(StrictModel):
    id: str
    statement_meaning: MeaningOracle
    expected_status: Literal["SUPPORTED", "CONTRADICTED", "NOT_FOUND"]
    fact_ids: list[str] = Field(min_length=1)
    relation_ids: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("milestone id must not be empty")
        return normalized

    @field_validator("fact_ids", "relation_ids")
    @classmethod
    def validate_refs(cls, value: list[str], info: Any) -> list[str]:
        normalized = [item.strip() for item in value if item.strip()]
        if len(normalized) != len(value) or len(set(normalized)) != len(normalized):
            raise ValueError(f"{info.field_name} must be non-empty and unique")
        return normalized


class ExpectedRelation(StrictModel):
    id: str
    operation: Literal["multiply", "sum", "absolute_difference", "greater_than"]
    input_fact_ids: list[str] = Field(min_length=2)
    output_fact_id: str = ""
    expected_boolean: bool | None = None

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("relation id must not be empty")
        return normalized

    @field_validator("input_fact_ids")
    @classmethod
    def validate_input_fact_ids(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if item.strip()]
        if len(normalized) != len(value) or len(set(normalized)) != len(normalized):
            raise ValueError("input_fact_ids must be non-empty and unique")
        return normalized

    @model_validator(mode="after")
    def validate_shape(self) -> "ExpectedRelation":
        self.output_fact_id = self.output_fact_id.strip()
        if self.operation in {"multiply", "absolute_difference"} and len(self.input_fact_ids) != 2:
            raise ValueError(f"{self.operation} relations require exactly two inputs")
        if self.operation == "greater_than":
            if len(self.input_fact_ids) != 2 or self.expected_boolean is None:
                raise ValueError("greater_than relations require two inputs and expected_boolean")
            if self.output_fact_id:
                raise ValueError("greater_than relations cannot declare output_fact_id")
        else:
            if not self.output_fact_id:
                raise ValueError(f"{self.operation} relations require output_fact_id")
            if self.expected_boolean is not None:
                raise ValueError(f"{self.operation} relations cannot declare expected_boolean")
        return self


class EpistemicBoundaryOracle(StrictModel):
    id: str
    statement_meaning: MeaningOracle
    allowed_statuses: list[Literal["SUPPORTED", "CONTRADICTED", "NOT_FOUND"]] = Field(min_length=1)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("epistemic boundary id must not be empty")
        return normalized

    @field_validator("allowed_statuses")
    @classmethod
    def validate_allowed_statuses(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("allowed_statuses must not contain duplicates")
        return value


class RequirementOracle(StrictModel):
    requirement_id: str
    decision_status: Literal["SUPPORTED", "CONTRADICTED", "NOT_FOUND"]
    projected_status: str
    blocking_obligations: bool


class CommunicationOracle(StrictModel):
    required_meanings: list[list[str]] = Field(default_factory=list)
    forbidden_phrases: list[str] = Field(default_factory=list)
    opposite_conclusions: list[MeaningOracle] = Field(default_factory=list)
    require_chinese: bool = True
    require_report_links: bool = True


class BusinessEvalOracle(StrictModel):
    schema_version: Literal["2"] = "2"
    case_id: str
    oracle_version: str
    sentinel: str
    facts: list[ExpectedFact] = Field(min_length=1)
    intent: TaskIntentOracle
    milestones: list[SemanticMilestoneOracle] = Field(min_length=1)
    relations: list[ExpectedRelation] = Field(min_length=1)
    epistemic_boundaries: list[EpistemicBoundaryOracle] = Field(default_factory=list)
    requirement: RequirementOracle
    communication: CommunicationOracle

    @model_validator(mode="after")
    def validate_oracle_graph(self) -> "BusinessEvalOracle":
        facts = _unique_by_id(self.facts, label="fact")
        milestones = _unique_by_id(self.milestones, label="milestone")
        relations = _unique_by_id(self.relations, label="relation")
        _unique_by_id(self.epistemic_boundaries, label="epistemic boundary")

        required_milestones = set(self.intent.required_milestone_ids)
        if required_milestones != set(milestones):
            missing = sorted(set(milestones) - required_milestones)
            unknown = sorted(required_milestones - set(milestones))
            raise ValueError(
                "intent must reference every milestone exactly once; "
                f"missing={missing}, unknown={unknown}"
            )

        referenced_relations: set[str] = set()
        for milestone in self.milestones:
            unknown_facts = sorted(set(milestone.fact_ids) - set(facts))
            unknown_relations = sorted(set(milestone.relation_ids) - set(relations))
            if unknown_facts or unknown_relations:
                raise ValueError(
                    f"milestone {milestone.id!r} has dangling refs; "
                    f"facts={unknown_facts}, relations={unknown_relations}"
                )
            referenced_relations.update(milestone.relation_ids)
        unreferenced_relations = sorted(set(relations) - referenced_relations)
        if unreferenced_relations:
            raise ValueError(f"relations must belong to a milestone: {unreferenced_relations}")

        for relation in self.relations:
            referenced_facts = set(relation.input_fact_ids)
            if relation.output_fact_id:
                referenced_facts.add(relation.output_fact_id)
            unknown_facts = sorted(referenced_facts - set(facts))
            if unknown_facts:
                raise ValueError(f"relation {relation.id!r} references unknown facts: {unknown_facts}")
            _validate_relation_truth(relation, facts)
        return self


def _unique_by_id(items: list[Any], *, label: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in items:
        item_id = str(item.id).strip()
        if not item_id:
            raise ValueError(f"{label} id must not be empty")
        if item_id in result:
            raise ValueError(f"duplicate {label} id: {item_id}")
        result[item_id] = item
    return result


def _decimal_fact(fact: ExpectedFact, *, relation_id: str) -> Decimal:
    if fact.kind != "decimal":
        raise ValueError(f"relation {relation_id!r} requires decimal fact {fact.id!r}")
    try:
        return Decimal(fact.value)
    except (ArithmeticError, ValueError) as exc:
        raise ValueError(f"relation {relation_id!r} has invalid decimal fact {fact.id!r}") from exc


def _validate_relation_truth(
    relation: ExpectedRelation,
    facts: dict[str, ExpectedFact],
) -> None:
    inputs = [_decimal_fact(facts[fact_id], relation_id=relation.id) for fact_id in relation.input_fact_ids]
    if relation.operation == "multiply":
        calculated = inputs[0] * inputs[1]
    elif relation.operation == "sum":
        calculated = sum(inputs, Decimal("0"))
    elif relation.operation == "absolute_difference":
        calculated = abs(inputs[0] - inputs[1])
    else:
        observed = inputs[0] > inputs[1]
        if observed != relation.expected_boolean:
            raise ValueError(
                f"relation {relation.id!r} is not self-consistent: "
                f"observed={observed}, expected={relation.expected_boolean}"
            )
        return

    output = facts[relation.output_fact_id]
    expected = _decimal_fact(output, relation_id=relation.id)
    if abs(calculated - expected) > output.tolerance:
        raise ValueError(
            f"relation {relation.id!r} is not self-consistent: "
            f"calculated={calculated}, expected={expected}, tolerance={output.tolerance}"
        )


class ReportArtifact(StrictModel):
    path: str
    kind: Literal["markdown", "pdf"]
    bytes: int = Field(ge=0)
    text: str = ""


class EvalSnapshot(StrictModel):
    schema_version: Literal["1"] = "1"
    case_id: str
    case_version: str
    run_id: str
    agent_commit: str = ""
    provider: str = ""
    model: str = ""
    policy_version: str = ""
    started_at: str = ""
    completed_at: str = ""
    case_state: dict[str, Any]
    conversation: list[dict[str, Any]] = Field(default_factory=list)
    reports: list[ReportArtifact] = Field(default_factory=list)
    trace: dict[str, Any] = Field(default_factory=dict)
    events: list[dict[str, Any]] = Field(default_factory=list)
    transcript_path: str = ""
    approved_tools: list[str] = Field(default_factory=list)
    runtime_error: str = ""


class ScoreCheck(StrictModel):
    id: str
    stage: Literal["understanding", "evidence", "reasoning", "proof", "report", "communication"]
    points: Decimal = Field(ge=0)
    earned: Decimal = Field(ge=0)
    passed: bool
    core: bool = False
    expected: Any = None
    observed: Any = None
    detail: str = ""


class EvalVeto(StrictModel):
    code: str
    detail: str


class EvalResult(StrictModel):
    schema_version: Literal["1"] = "1"
    case_id: str
    case_version: str
    run_id: str
    scorer_version: str
    passed: bool
    score: Decimal = Field(ge=0, le=100)
    raw_score: Decimal | None = Field(default=None, ge=0, le=100)
    score_cap: Decimal = Field(default=Decimal("100"), ge=0, le=100)
    score_cap_reason: str = ""
    first_failed_stage: str = ""
    checks: list[ScoreCheck]
    vetoes: list[EvalVeto] = Field(default_factory=list)
    engineering: dict[str, Any] = Field(default_factory=dict)


def load_case(case_dir: Path) -> BusinessEvalCase:
    return BusinessEvalCase.model_validate_json((case_dir / "case.json").read_text(encoding="utf-8"))


def load_oracle(case_dir: Path) -> BusinessEvalOracle:
    return BusinessEvalOracle.model_validate_json((case_dir / "oracle.json").read_text(encoding="utf-8"))


def validate_case_input(case_dir: Path) -> BusinessEvalCase:
    case_dir = case_dir.resolve()
    case = load_case(case_dir)
    ids: set[str] = set()
    for attachment in case.attachments:
        if attachment.id in ids:
            raise ValueError(f"duplicate attachment id: {attachment.id}")
        ids.add(attachment.id)
        path = (case_dir / attachment.path).resolve()
        if case_dir not in path.parents or not path.is_file():
            raise ValueError(f"attachment escapes case directory or is missing: {attachment.path}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != attachment.sha256:
            raise ValueError(f"attachment hash mismatch: {attachment.id}")
    return case


def validate_case_bundle(case_dir: Path) -> tuple[BusinessEvalCase, BusinessEvalOracle]:
    case_dir = case_dir.resolve()
    case = validate_case_input(case_dir)
    oracle = load_oracle(case_dir)
    if oracle.case_id != case.case_id:
        raise ValueError("case.json and oracle.json case_id must match")
    if oracle.sentinel in json.dumps(case.model_dump(mode="json"), ensure_ascii=False):
        raise ValueError("oracle sentinel leaked into case.json")
    return case, oracle
