from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"


class UserTurnSpec(BaseModel):
    message: str
    attach: list[str] = Field(default_factory=list)
    approvals: dict[str, bool] = Field(default_factory=dict)
    manager_decisions: list[dict[str, Any]] = Field(default_factory=list)


class ScenarioSpec(BaseModel):
    id: str
    name: str = ""
    description: str = ""
    case_type: str = "invoice_payment_review"
    tags: list[str] = Field(default_factory=list)
    user_script: list[UserTurnSpec]
    scripted_roles: dict[str, list[dict[str, Any]] | dict[str, Any]] = Field(default_factory=dict)
    initial_case_state: dict[str, Any] | None = None
    budgets: dict[str, float] = Field(default_factory=dict)
    judge: dict[str, Any] = Field(default_factory=dict)


class ExpectedSpec(BaseModel):
    reply_contains: list[str] = Field(default_factory=list)
    reply_must_not_contain: list[str] = Field(default_factory=list)
    requirements: dict[str, str] = Field(default_factory=dict)
    must_have_risk_flags: list[str] = Field(default_factory=list)
    must_not_have_risk_flags: list[str] = Field(default_factory=list)
    must_have_evidence_types: list[str] = Field(default_factory=list)
    forbidden_requirements: dict[str, str] = Field(default_factory=dict)
    trace_must_call: list[str] = Field(default_factory=list)
    trace_must_not_call: list[str] = Field(default_factory=list)
    trace_must_approve: list[str] = Field(default_factory=list)
    artifacts_must_exist: list[str] = Field(default_factory=list)
    artifacts_must_not_exist: list[str] = Field(default_factory=list)
    rag_expected_profile_ids: list[str] = Field(default_factory=list)
    rag_expected_source_terms: list[str] = Field(default_factory=list)
    safety: dict[str, Any] = Field(default_factory=dict)
    budgets: dict[str, float] = Field(default_factory=dict)


class CheckResult(BaseModel):
    name: str
    passed: bool
    score: float = 1.0
    details: dict[str, Any] = Field(default_factory=dict)


class LlmJudgeResult(BaseModel):
    enabled: bool = False
    passed: bool = True
    score: float = 0.0
    rationale: str = ""
    dimensions: dict[str, float] = Field(default_factory=dict)
    error: str = ""


class ScenarioRunResult(BaseModel):
    scenario_id: str
    run_index: int = 1
    mode: Literal["scripted", "live"] = "scripted"
    passed: bool = False
    score: float = 0.0
    checks: list[CheckResult] = Field(default_factory=list)
    llm_judge: LlmJudgeResult = Field(default_factory=LlmJudgeResult)
    case_id: str = ""
    run_ids: list[str] = Field(default_factory=list)
    final_reply: str = ""
    case_state: dict[str, Any] = Field(default_factory=dict)
    trace: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    report_dir: str = ""


class BenchmarkSummary(BaseModel):
    benchmark: str = "InvoiceTauBench"
    mode: str = "scripted"
    scenario_count: int = 0
    total_runs: int = 0
    passed_runs: int = 0
    pass_at_1: float = 0.0
    pass_all_k: float = 0.0
    average_score: float = 0.0
    total_tokens: int = 0
    total_wall_time_ms: float = 0.0
    scenario_results: list[ScenarioRunResult] = Field(default_factory=list)
