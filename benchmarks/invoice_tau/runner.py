from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from .bootstrap import ensure_paths

ensure_paths()

from .io import load_scenario, write_json
from .judge import run_llm_judge
from .models import ExpectedSpec, REPO_ROOT, ScenarioRunResult, ScenarioSpec, UserTurnSpec
from .scripted import ScriptedManagerRunner, ScriptedRoleRegistry
from .verifiers import score_checks, verify_run

from app.config import get_settings  # noqa: E402
from app.runtime.turn_runner import AgentRuntime  # noqa: E402
from app.state.case_store import CaseStore  # noqa: E402
from app.state.schemas import AgentTurnRequest, Attachment, CaseState  # noqa: E402


SETTINGS_ENV_KEYS = (
    "INVOICE_AGENT_WORKSPACE_ROOT",
    "INVOICE_AGENT_STORAGE_ROOT",
    "INVOICE_AGENT_SESSION_DB",
    "INVOICE_AGENT_MEMORY_DB",
)
MAX_APPROVAL_RESUMES = 8


class InvoiceTauBenchRunner:
    def __init__(
        self,
        *,
        report_dir: Path,
        mode: str = "scripted",
        llm_judge: bool = False,
        require_llm_judge: bool = False,
        default_approval: bool = False,
    ) -> None:
        self.report_dir = report_dir
        self.mode = mode
        self.llm_judge = llm_judge
        self.require_llm_judge = require_llm_judge
        self.default_approval = default_approval
        self.workspace_root = report_dir / "workspace" / "cases"
        self.storage_root = report_dir / "storage"

    def run_path(self, scenario_path: Path, *, run_index: int = 1) -> ScenarioRunResult:
        scenario, expected, scenario_dir = load_scenario(scenario_path)
        return self.run_scenario(scenario, expected, scenario_dir=scenario_dir, run_index=run_index)

    def run_scenario(
        self,
        scenario: ScenarioSpec,
        expected: ExpectedSpec,
        *,
        scenario_dir: Path,
        run_index: int = 1,
    ) -> ScenarioRunResult:
        original_env = _capture_settings_env()
        try:
            return self._run_scenario_isolated(
                scenario,
                expected,
                scenario_dir=scenario_dir,
                run_index=run_index,
            )
        finally:
            _restore_settings_env(original_env)

    def _run_scenario_isolated(
        self,
        scenario: ScenarioSpec,
        expected: ExpectedSpec,
        *,
        scenario_dir: Path,
        run_index: int = 1,
    ) -> ScenarioRunResult:
        started = time.perf_counter()
        case_id = f"bench_{_safe_id(scenario.id)}_{run_index}"
        _reset_settings(self.workspace_root, self.storage_root)
        case_store = CaseStore(self.workspace_root)
        if scenario.initial_case_state:
            state_payload = dict(scenario.initial_case_state)
            state_payload["case_id"] = case_id
            case_store.save(CaseState.model_validate(state_payload))
        manager = None
        if self.mode == "scripted":
            manager = ScriptedManagerRunner([turn.manager_decisions for turn in scenario.user_script])
        runtime = AgentRuntime(store=case_store, manager_runner=manager)
        if self.mode == "scripted":
            runtime.runner.roles = ScriptedRoleRegistry(scenario.scripted_roles)

        responses = []
        error = ""
        try:
            for turn in scenario.user_script:
                response = runtime.run_turn(
                    AgentTurnRequest(
                        case_id=case_id,
                        message=turn.message,
                        attachments=_attachments_for_turn(turn, scenario_dir),
                    )
                )
                responses.append(response)
                response = self._resume_approval_interrupts(runtime, case_id, response, turn, responses)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

        case_dir = self.workspace_root / case_id
        final_response = responses[-1] if responses else None
        trace = _merged_trace([response.trace for response in responses])
        events = _read_events(case_dir)
        trace["events"] = events
        case_state = final_response.case_state.model_dump(mode="json") if final_response else _load_case_state(case_store, case_id)
        final_reply = final_response.reply if final_response else error
        metrics = _metrics(trace, events, started, case_state=case_state, case_dir=case_dir)
        metrics["scenario_budgets"] = dict(scenario.budgets or {})
        metrics["error"] = error
        result = ScenarioRunResult(
            scenario_id=scenario.id,
            run_index=run_index,
            mode=self.mode,  # type: ignore[arg-type]
            category=_category_for(scenario),
            case_id=case_id,
            run_ids=_run_ids(trace, events),
            final_reply=final_reply,
            case_state=case_state,
            trace=trace,
            metrics=metrics,
            report_dir=str(self.report_dir.relative_to(REPO_ROOT) if self.report_dir.is_relative_to(REPO_ROOT) else self.report_dir),
        )
        checks = verify_run(result, expected, case_dir)
        if error:
            from .models import CheckResult

            checks.append(CheckResult(name="runtime_error", passed=False, score=0.0, details={"error": error}))
        passed, score = score_checks(checks)
        result.checks = checks
        result.passed = passed
        result.score = score
        judge = run_llm_judge(result, expected, enabled=self.llm_judge or bool(scenario.judge.get("enabled")))
        result.llm_judge = judge
        if self.require_llm_judge and judge.enabled and not judge.error:
            result.passed = bool(result.passed and judge.passed)
            result.score = round((result.score + judge.score) / 2, 4)
        write_json(self.report_dir / "runs" / f"{scenario.id}_{run_index}.json", result.model_dump(mode="json"))
        return result

    def _resume_approval_interrupts(
        self,
        runtime: AgentRuntime,
        case_id: str,
        response: Any,
        turn: UserTurnSpec,
        responses: list[Any],
    ) -> Any:
        resume_count = 0
        while response.trace.get("interrupts"):
            resume_count += 1
            if resume_count > MAX_APPROVAL_RESUMES:
                raise RuntimeError(f"Approval resume loop exceeded {MAX_APPROVAL_RESUMES} iterations for {case_id}.")
            run_id = str(response.trace.get("run_id") or "")
            if not run_id:
                raise RuntimeError(f"Approval interrupt for {case_id} did not include run_id.")
            approvals = list(response.trace.get("interrupts") or [])
            if not approvals:
                break
            approval = approvals[0]
            tool = str(approval.get("tool") or "") if isinstance(approval, dict) else ""
            approved = bool(turn.approvals.get(tool, self.default_approval))
            response = runtime.resume_approval(
                case_id=case_id,
                run_id=run_id,
                approved=approved,
                reason=f"invoice_tau_bench approved={approved} tool={tool}",
            )
            responses.append(response)
        return response


def _reset_settings(workspace_root: Path, storage_root: Path) -> None:
    os.environ["INVOICE_AGENT_WORKSPACE_ROOT"] = str(workspace_root)
    os.environ["INVOICE_AGENT_STORAGE_ROOT"] = str(storage_root)
    os.environ["INVOICE_AGENT_SESSION_DB"] = str(storage_root / "sessions.sqlite")
    os.environ["INVOICE_AGENT_MEMORY_DB"] = str(storage_root / "memory.sqlite")
    get_settings.cache_clear()


def _capture_settings_env() -> dict[str, str | None]:
    return {key: os.environ.get(key) for key in SETTINGS_ENV_KEYS}


def _restore_settings_env(values: dict[str, str | None]) -> None:
    for key in SETTINGS_ENV_KEYS:
        value = values.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()


def _attachments_for_turn(turn: UserTurnSpec, scenario_dir: Path) -> list[Attachment]:
    attachments: list[Attachment] = []
    scenario_root = scenario_dir.resolve()
    for name in turn.attach:
        direct_path = (scenario_dir / name).resolve()
        legacy_path = (scenario_dir / "attachments" / name).resolve()
        path = direct_path if direct_path.is_file() else legacy_path
        if not path.is_relative_to(scenario_root):
            raise ValueError(f"Attachment must stay inside the scenario directory: {name}")
        if not path.is_file():
            raise FileNotFoundError(f"Attachment not found: {path}")
        attachments.append(Attachment(name=path.name, path=str(path), content_type=_content_type(path)))
    return attachments


def _content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".md":
        return "text/markdown"
    if suffix == ".txt":
        return "text/plain"
    if suffix == ".json":
        return "application/json"
    if suffix == ".pdf":
        return "application/pdf"
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    return "application/octet-stream"


def _merged_trace(traces: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    list_keys = [
        "planner_actions",
        "supervisor_decisions",
        "model_calls",
        "tool_calls",
        "role_calls",
        "observations",
        "trace_checkpoints",
        "checkpoints",
        "interrupts",
    ]
    for trace in traces:
        if not trace:
            continue
        merged.update({key: value for key, value in trace.items() if key not in list_keys})
        for key in list_keys:
            merged.setdefault(key, [])
            merged[key].extend(trace.get(key) or [])
    for key in list_keys:
        merged[key] = _dedupe_json_rows(merged.get(key) or [])
    return merged


def _dedupe_json_rows(rows: list[Any]) -> list[Any]:
    seen: set[str] = set()
    result: list[Any] = []
    for row in rows:
        key = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def _read_events(case_dir: Path) -> list[dict[str, Any]]:
    path = case_dir / "traces" / "events.jsonl"
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events


def _metrics(
    trace: dict[str, Any],
    events: list[dict[str, Any]],
    started: float,
    *,
    case_state: dict[str, Any],
    case_dir: Path,
) -> dict[str, Any]:
    model_calls = trace.get("model_calls") or []
    provider_calls = [event for event in events if event.get("kind") == "provider_call"]
    tool_calls = trace.get("tool_calls") or []
    role_calls = trace.get("role_calls") or []
    total_tokens = 0
    input_tokens = 0
    output_tokens = 0
    usage_events = provider_calls or [event for event in events if event.get("kind") == "model_call"]
    for event in usage_events:
        usage = ((event.get("payload") or {}).get("usage") or {}) if isinstance(event.get("payload"), dict) else {}
        input_tokens += int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        output_tokens += int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        total_tokens += int(usage.get("total_tokens") or 0)
    if not total_tokens:
        total_tokens = input_tokens + output_tokens
    requirement_status = {
        str(item.get("id") or ""): str(item.get("status") or "")
        for item in case_state.get("requirements") or []
        if isinstance(item, dict)
    }
    return {
        "wall_time_ms": round((time.perf_counter() - started) * 1000, 2),
        "final_status": str(case_state.get("status") or ""),
        "missing_requirements": sorted(key for key, status in requirement_status.items() if status == "missing"),
        "conflict_requirements": sorted(key for key, status in requirement_status.items() if status == "conflict"),
        "evidence_count": len(case_state.get("evidence_items") or []),
        "model_calls": len(provider_calls) if provider_calls else len(model_calls),
        "tool_calls": len(tool_calls),
        "role_calls": len(role_calls),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "approval_interrupts": len([event for event in events if event.get("kind") == "approval_interrupt"]),
        "approval_decisions": len([event for event in events if event.get("kind") == "approval_decision"]),
        "rag_events": len([event for event in events if event.get("kind") == "rag_guidance"]),
        "artifact_summary": _artifact_summary(case_dir),
        "events": events,
    }


def _run_ids(trace: dict[str, Any], events: list[dict[str, Any]]) -> list[str]:
    values = [str(trace.get("run_id") or "")]
    values.extend(str(event.get("run_id") or "") for event in events)
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _load_case_state(store: CaseStore, case_id: str) -> dict[str, Any]:
    try:
        return store.load(case_id).model_dump(mode="json")
    except Exception:
        return {}


def _safe_id(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in value)[:80]


def _category_for(scenario: ScenarioSpec) -> str:
    tags = set(scenario.tags or [])
    if "create_case" in tags:
        return "create_case"
    if "chat" in tags:
        return "chat"
    if "materials_advisor" in tags:
        return "material_help"
    if "attachments" in tags:
        return "material_review"
    if "report" in tags:
        return "report"
    return "general"


def _artifact_summary(case_dir: Path) -> dict[str, int]:
    if not case_dir.exists():
        return {"files": 0, "reports": 0, "pdfs": 0, "traces": 0}
    files = [path for path in case_dir.rglob("*") if path.is_file()]
    return {
        "files": len(files),
        "reports": len([path for path in files if "reports" in path.parts]),
        "pdfs": len([path for path in files if path.suffix.lower() == ".pdf"]),
        "traces": len([path for path in files if "traces" in path.parts]),
    }


def clean_report_dir(report_dir: Path) -> None:
    if report_dir.exists():
        shutil.rmtree(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
