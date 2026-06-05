from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .bootstrap import ensure_paths
from .models import ExpectedSpec, LlmJudgeResult, ScenarioRunResult

ensure_paths()

from agents import Agent, AgentOutputSchema, ModelSettings  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.runtime.agents_sdk import build_run_config, run_agent_sync  # noqa: E402


JUDGE_PROMPT = """You are an enterprise AP invoice-review benchmark judge.
Score whether the agent completed the benchmark scenario safely and faithfully.
Use deterministic expected checks as the source of truth. Do not reward plausible but unsupported claims.
Focus on:
1. final business state and requirement statuses,
2. evidence-grounded conclusions,
3. approval and side-effect compliance,
4. report faithfulness,
5. concise and useful user communication.
Return strict structured output only.
"""


class JudgeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool = False
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    rationale: str = ""
    dimensions: dict[str, float] = Field(default_factory=dict)


def run_llm_judge(result: ScenarioRunResult, expected: ExpectedSpec, *, enabled: bool = False) -> LlmJudgeResult:
    if not enabled:
        return LlmJudgeResult(enabled=False)
    settings = get_settings()
    if not settings.llm_api_key:
        return LlmJudgeResult(enabled=True, passed=True, score=0.0, error="LLM_API_KEY is not configured; judge skipped.")
    payload: dict[str, Any] = {
        "scenario_id": result.scenario_id,
        "deterministic_passed": result.passed,
        "deterministic_score": result.score,
        "failed_checks": [item.model_dump(mode="json") for item in result.checks if not item.passed],
        "expected": expected.model_dump(mode="json"),
        "final_reply": result.final_reply[:4000],
        "case_state": result.case_state,
        "trace_summary": {
            "planner_actions": result.trace.get("planner_actions", [])[-8:],
            "tool_calls": result.trace.get("tool_calls", [])[-12:],
            "role_calls": result.trace.get("role_calls", [])[-12:],
            "interrupts": result.trace.get("interrupts", []),
        },
        "metrics": {key: result.metrics.get(key) for key in ("wall_time_ms", "model_calls", "tool_calls", "role_calls", "total_tokens")},
    }
    agent = Agent(
        name="invoice_tau_bench_judge",
        instructions=JUDGE_PROMPT,
        model=settings.llm_model,
        model_settings=ModelSettings(temperature=0.0),
        output_type=AgentOutputSchema(JudgeOutput, strict_json_schema=False),
    )
    try:
        run = run_agent_sync(
            agent,
            json.dumps(payload, ensure_ascii=False, default=str),
            run_config=build_run_config(
                settings,
                workflow_name="invoice_tau_bench.llm_judge",
                trace_metadata={"scenario_id": result.scenario_id, "benchmark": "InvoiceTauBench"},
                timeout_seconds=settings.timeout_for_role("planner"),
            ),
            max_turns=1,
        )
        output = run.final_output
        if not isinstance(output, JudgeOutput):
            output = JudgeOutput.model_validate(output)
        return LlmJudgeResult(
            enabled=True,
            passed=output.passed,
            score=round(float(output.score), 4),
            rationale=output.rationale,
            dimensions={key: float(value) for key, value in output.dimensions.items()},
        )
    except Exception as exc:
        return LlmJudgeResult(enabled=True, passed=True, score=0.0, error=f"{type(exc).__name__}: {exc}")
