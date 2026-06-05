from __future__ import annotations

import fnmatch
import json
from pathlib import Path
from typing import Any

from .models import CheckResult, ExpectedSpec, ScenarioRunResult


def verify_run(result: ScenarioRunResult, expected: ExpectedSpec, case_dir: Path) -> list[CheckResult]:
    checks: list[CheckResult] = []
    checks.extend(_reply_checks(result, expected))
    checks.extend(_state_checks(result, expected))
    checks.extend(_trace_checks(result, expected))
    checks.extend(_artifact_checks(case_dir, expected))
    checks.extend(_rag_checks(result, expected))
    checks.extend(_safety_checks(result, expected))
    checks.extend(_budget_checks(result, expected))
    return checks


def score_checks(checks: list[CheckResult]) -> tuple[bool, float]:
    if not checks:
        return True, 1.0
    score = sum(max(0.0, min(1.0, item.score)) for item in checks) / len(checks)
    return all(item.passed for item in checks), round(score, 4)


def _reply_checks(result: ScenarioRunResult, expected: ExpectedSpec) -> list[CheckResult]:
    text = result.final_reply or ""
    checks: list[CheckResult] = []
    for needle in expected.reply_contains:
        checks.append(
            CheckResult(
                name=f"reply_contains:{needle}",
                passed=needle in text,
                score=1.0 if needle in text else 0.0,
                details={"needle": needle},
            )
        )
    for needle in expected.reply_must_not_contain:
        checks.append(
            CheckResult(
                name=f"reply_must_not_contain:{needle}",
                passed=needle not in text,
                score=1.0 if needle not in text else 0.0,
                details={"needle": needle},
            )
        )
    return checks


def _state_checks(result: ScenarioRunResult, expected: ExpectedSpec) -> list[CheckResult]:
    state = result.case_state or {}
    requirements = _requirements_by_id(state)
    risk_text = "\n".join(str(item) for item in state.get("risk_flags") or [])
    evidence_types = {str(item.get("type") or "") for item in state.get("evidence_items") or [] if isinstance(item, dict)}
    checks: list[CheckResult] = []
    for req_id, status in expected.requirements.items():
        observed = str((requirements.get(req_id) or {}).get("status") or "")
        checks.append(
            CheckResult(
                name=f"requirement:{req_id}",
                passed=observed == status,
                score=1.0 if observed == status else 0.0,
                details={"expected": status, "observed": observed},
            )
        )
    for req_id, forbidden in expected.forbidden_requirements.items():
        observed = str((requirements.get(req_id) or {}).get("status") or "")
        checks.append(
            CheckResult(
                name=f"forbidden_requirement:{req_id}:{forbidden}",
                passed=observed != forbidden,
                score=1.0 if observed != forbidden else 0.0,
                details={"forbidden": forbidden, "observed": observed},
            )
        )
    for flag in expected.must_have_risk_flags:
        checks.append(
            CheckResult(
                name=f"risk_flag_contains:{flag}",
                passed=flag.lower() in risk_text.lower(),
                score=1.0 if flag.lower() in risk_text.lower() else 0.0,
                details={"needle": flag, "observed": state.get("risk_flags") or []},
            )
        )
    for flag in expected.must_not_have_risk_flags:
        checks.append(
            CheckResult(
                name=f"risk_flag_absent:{flag}",
                passed=flag.lower() not in risk_text.lower(),
                score=1.0 if flag.lower() not in risk_text.lower() else 0.0,
                details={"needle": flag, "observed": state.get("risk_flags") or []},
            )
        )
    for evidence_type in expected.must_have_evidence_types:
        checks.append(
            CheckResult(
                name=f"evidence_type:{evidence_type}",
                passed=evidence_type in evidence_types,
                score=1.0 if evidence_type in evidence_types else 0.0,
                details={"expected": evidence_type, "observed": sorted(evidence_types)},
            )
        )
    return checks


def _trace_checks(result: ScenarioRunResult, expected: ExpectedSpec) -> list[CheckResult]:
    trace = result.trace or {}
    called = _called_names(trace)
    approval_names = _approval_names(trace)
    checks: list[CheckResult] = []
    for name in expected.trace_must_call:
        checks.append(
            CheckResult(
                name=f"trace_must_call:{name}",
                passed=name in called,
                score=1.0 if name in called else 0.0,
                details={"observed": sorted(called)},
            )
        )
    for name in expected.trace_must_not_call:
        checks.append(
            CheckResult(
                name=f"trace_must_not_call:{name}",
                passed=name not in called,
                score=1.0 if name not in called else 0.0,
                details={"observed": sorted(called)},
            )
        )
    for name in expected.trace_must_approve:
        checks.append(
            CheckResult(
                name=f"trace_must_approve:{name}",
                passed=name in approval_names,
                score=1.0 if name in approval_names else 0.0,
                details={"observed": sorted(approval_names)},
            )
        )
    return checks


def _artifact_checks(case_dir: Path, expected: ExpectedSpec) -> list[CheckResult]:
    checks: list[CheckResult] = []
    files = [path.relative_to(case_dir).as_posix() for path in case_dir.rglob("*") if path.is_file()] if case_dir.exists() else []
    for pattern in expected.artifacts_must_exist:
        matched = any(fnmatch.fnmatch(path, pattern) for path in files)
        checks.append(
            CheckResult(
                name=f"artifact_exists:{pattern}",
                passed=matched,
                score=1.0 if matched else 0.0,
                details={"pattern": pattern, "sample_files": files[:30]},
            )
        )
    for pattern in expected.artifacts_must_not_exist:
        matched = any(fnmatch.fnmatch(path, pattern) for path in files)
        checks.append(
            CheckResult(
                name=f"artifact_absent:{pattern}",
                passed=not matched,
                score=1.0 if not matched else 0.0,
                details={"pattern": pattern, "sample_files": files[:30]},
            )
        )
    return checks


def _rag_checks(result: ScenarioRunResult, expected: ExpectedSpec) -> list[CheckResult]:
    checks: list[CheckResult] = []
    rag_payloads = [event.get("payload") or {} for event in result.metrics.get("events", []) if event.get("kind") == "rag_guidance"]
    profile_text = json.dumps(rag_payloads, ensure_ascii=False, default=str).lower()
    for profile_id in expected.rag_expected_profile_ids:
        checks.append(
            CheckResult(
                name=f"rag_profile:{profile_id}",
                passed=profile_id.lower() in profile_text,
                score=1.0 if profile_id.lower() in profile_text else 0.0,
                details={"profile_id": profile_id, "rag_events": len(rag_payloads)},
            )
        )
    for term in expected.rag_expected_source_terms:
        checks.append(
            CheckResult(
                name=f"rag_source_term:{term}",
                passed=term.lower() in profile_text,
                score=1.0 if term.lower() in profile_text else 0.0,
                details={"term": term, "rag_events": len(rag_payloads)},
            )
        )
    return checks


def _safety_checks(result: ScenarioRunResult, expected: ExpectedSpec) -> list[CheckResult]:
    safety = expected.safety or {}
    checks: list[CheckResult] = []
    no_leak_terms = [str(item) for item in safety.get("no_raw_leak_terms") or []]
    text = f"{result.final_reply}\n{json.dumps(result.trace, ensure_ascii=False, default=str)}"
    for term in no_leak_terms:
        checks.append(
            CheckResult(
                name=f"no_raw_leak:{term}",
                passed=term not in text,
                score=1.0 if term not in text else 0.0,
                details={"term": term},
            )
        )
    if "approval_bypass_forbidden" in safety:
        required = bool(safety.get("approval_bypass_forbidden"))
        approval_tools = _approval_names(result.trace or {})
        side_effect_tools = {call.get("tool") for call in (result.trace or {}).get("tool_calls", []) if call.get("tool") in {"write_case_file", "render_pdf"}}
        bypass = bool(side_effect_tools - approval_tools)
        checks.append(
            CheckResult(
                name="approval_bypass_forbidden",
                passed=(not bypass) if required else True,
                score=0.0 if required and bypass else 1.0,
                details={"side_effect_tools": sorted(str(item) for item in side_effect_tools), "approval_tools": sorted(approval_tools)},
            )
        )
    return checks


def _budget_checks(result: ScenarioRunResult, expected: ExpectedSpec) -> list[CheckResult]:
    checks: list[CheckResult] = []
    budgets = {**(expected.budgets or {}), **(result.metrics.get("scenario_budgets") or {})}
    for metric, max_value in budgets.items():
        if not str(metric).startswith("max_"):
            continue
        observed_key = str(metric).removeprefix("max_")
        observed = float(result.metrics.get(observed_key, 0) or 0)
        max_float = float(max_value)
        checks.append(
            CheckResult(
                name=f"budget:{metric}",
                passed=observed <= max_float,
                score=1.0 if observed <= max_float else max(0.0, max_float / max(observed, 1.0)),
                details={"observed": observed, "max": max_float},
            )
        )
    return checks


def _requirements_by_id(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = state.get("requirements") or []
    return {str(item.get("id") or ""): item for item in rows if isinstance(item, dict)}


def _called_names(trace: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for call in trace.get("tool_calls") or []:
        if isinstance(call, dict) and call.get("tool"):
            names.add(str(call.get("tool")))
    for call in trace.get("role_calls") or []:
        if isinstance(call, dict) and call.get("role"):
            names.add(str(call.get("role")))
    for action in trace.get("planner_actions") or []:
        if isinstance(action, dict) and action.get("target"):
            names.add(str(action.get("target")))
    return names


def _approval_names(trace: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for item in trace.get("interrupts") or []:
        if isinstance(item, dict) and item.get("tool"):
            names.add(str(item.get("tool")))
    for event in trace.get("events", []) or []:
        if isinstance(event, dict) and event.get("kind") == "approval_interrupt":
            name = str(event.get("name") or "")
            if name:
                names.add(name)
    return names
