from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.runtime.turn_runner import AgentRuntime  # noqa: E402
from app.state.session_repository import SessionRepository  # noqa: E402
from app.state.case_store import CaseStore  # noqa: E402
from app.state.schemas import AgentTurnRequest, Attachment  # noqa: E402


SCENARIO_DIR = BACKEND / "evals" / "scenarios"
REPORT_DIR = ROOT / "reports" / "evals"
WORKSPACE = ROOT / "workspace" / "cases"
CORE_REQUIREMENTS = {
    "invoice",
    "purchase_order",
    "goods_receipt_or_service_acceptance",
    "vendor_identity",
    "duplicate_payment_screen",
}
REQUIREMENT_ALIASES = {
    "invoice": {"invoice"},
    "purchase_order": {"purchase_order"},
    "goods_receipt_or_service_acceptance": {"goods_receipt_or_service_acceptance", "goods_receipt"},
    "vendor_identity": {"vendor_identity", "vendor_record"},
    "duplicate_payment_screen": {"duplicate_payment_screen", "duplicate_payment_check"},
}
COMPLETE_CLAIM_PATTERNS = (
    re.compile(r"(?:五项|全部|所有|核心|必需).{0,10}(?:均|都|全部)?(?:已|已经)?(?:满足|齐全|通过)"),
    re.compile(r"(?:均|全部|全都)(?:已|已经)?满足"),
    re.compile(r"证据链完整(?!性|后)"),
    re.compile(r"(?<!待)材料(?:已|已经)?齐全(?!后)"),
    re.compile(r"全部齐全"),
    re.compile(r"可供(?:人工)?主管?复核(?:参考)?"),
    re.compile(r"(?:状态|status)\s*(?:为|=|已更新为)\s*ready_for_report", re.I),
)
NEGATED_COMPLETE_CLAIM_PATTERNS = (
    re.compile(r"(?:未|尚未|不|不能|缺失|缺少|缺口|不足).{0,12}(?:满足|齐全|通过|完整)"),
    re.compile(r"(?:五项|全部|所有|核心|必需).{0,12}(?:未|尚未|不|不能|缺失|缺少|缺口|不足)"),
)
EXECUTION_CLAIM_PATTERNS = (
    re.compile(r"\b(?:has\s+been\s+)?(?:paid|posted|approved|submitted)\b", re.I),
    re.compile(r"\bapproved\s+for\s+payment\b", re.I),
    re.compile(r"\bready\s+for\s+payment\b", re.I),
    re.compile(r"\bsubmit(?:ted)?\s+(?:to\s+)?erp\b", re.I),
    re.compile(r"已付款|已支付|已批准|已审批|已提交\s*ERP|已过账|已清账"),
    re.compile(r"可直接?付款|可付款|可支付|可审批|可批准|可提交|提交\s*ERP|推进付款审批"),
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", action="append", default=None)
    parser.add_argument("--manual", action="store_true")
    args = parser.parse_args()

    scenarios = load_scenarios(args.scenario or ["all"])
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for scenario in scenarios:
        result = run_scenario(scenario, manual=args.manual)
        results.append(result)
        print_result(result)
    write_reports(results)


def load_scenarios(selectors: str | list[str]) -> list[dict[str, Any]]:
    if isinstance(selectors, str):
        raw_selectors = [selectors]
    else:
        raw_selectors = selectors
    expanded = [
        part.strip()
        for selector in raw_selectors
        for part in str(selector).split(",")
        if part.strip()
    ] or ["all"]
    files = sorted(SCENARIO_DIR.glob("*.json"))
    if "all" not in expanded:
        files = [path for path in files if any(selector in path.stem or selector == path.stem for selector in expanded)]
    if not files:
        raise SystemExit(f"No scenarios found for {', '.join(expanded)}")
    return [json.loads(path.read_text(encoding="utf-8")) for path in files]


def run_scenario(scenario: dict[str, Any], *, manual: bool = False) -> dict[str, Any]:
    case_id = str(scenario.get("case_id"))
    case_dir = WORKSPACE / case_id
    if case_dir.exists():
        shutil.rmtree(case_dir)
    store = CaseStore(WORKSPACE)
    runtime = AgentRuntime(store=store)
    step_results = []
    all_actions: list[str] = []
    all_errors: list[str] = []
    planner_raw_hits: list[str] = []
    role_context_hits: list[str] = []

    for index, step in enumerate(scenario.get("steps") or [], start=1):
        message = build_message(step)
        attachments = [
            Attachment(name=Path(path).name, path=str((ROOT / path).resolve()))
            for path in step.get("attachments", [])
        ]
        response = runtime.run_turn(AgentTurnRequest(case_id=case_id, message=message, attachments=attachments))
        trace = read_trace(case_id, response.trace["run_id"])
        actions = [action_label(item) for item in response.trace.get("planner_actions", [])]
        all_actions.extend(actions)
        errors = collect_errors(trace)
        all_errors.extend(errors)
        planner_raw_hits.extend(raw_leaks(trace, scenario.get("raw_leak_phrases") or []))
        role_context_hits.extend(role_context_checks(trace))
        step_results.append(
            {
                "step": index,
                "message": message[:500],
                "attachments": [item.name for item in attachments],
                "action_chain": actions,
                "step_count": response.trace.get("step_count"),
                "model_calls": len(trace.get("model_calls", [])),
                "errors": errors,
                "reply": response.reply,
                "case_state": summarize_case_state(response.case_state),
                "trace_id": response.trace["run_id"],
            }
        )
        if scenario.get("force_compact_after") == index:
            runtime.runner.sessions.compact_before_run(
                case_id,
                force=True,
                reason="eval_force",
                estimated_context_chars=0,
                context_char_limit=1,
            )

    session = SessionRepository(store).load_session(case_id)
    final_state = store.load(case_id)
    generated_files = list_generated_files(case_id)
    pass_fail, bug_notes = evaluate_expectations(
        scenario=scenario,
        action_chain=all_actions,
        errors=all_errors,
        planner_raw_hits=planner_raw_hits,
        role_context_hits=role_context_hits,
        final_state=final_state,
        generated_files=generated_files,
        session=session,
        step_results=step_results,
    )
    result = {
        "scenario": scenario.get("name"),
        "case_id": case_id,
        "pass": pass_fail,
        "bug_notes": bug_notes,
        "action_chain": all_actions,
        "planner_input_raw_leak_check": {
            "raw_hits": sorted(set(planner_raw_hits)),
            "passed": not planner_raw_hits,
        },
        "role_context_check": {
            "hits": sorted(set(role_context_hits)),
            "passed": bool(role_context_hits) if "role_context_contains" in scenario.get("expect", {}) else True,
        },
        "case_state_delta": summarize_case_state(final_state),
        "final_reply": step_results[-1]["reply"] if step_results else "",
        "generated_files": generated_files,
        "session": {
            "session_json_exists": (case_dir / "session.json").exists(),
            "turn_count": len(session.get("turns") or []),
            "session_summary": session.get("session_summary", ""),
            "compact_history_count": len(session.get("compact_history") or []),
        },
        "steps": step_results,
    }
    if manual:
        result["manual_scores"] = collect_manual_scores(result)
    return result


def evaluate_expectations(
    *,
    scenario: dict[str, Any],
    action_chain: list[str],
    errors: list[str],
    planner_raw_hits: list[str],
    role_context_hits: list[str],
    final_state: Any,
    generated_files: list[str],
    session: dict[str, Any],
    step_results: list[dict[str, Any]],
) -> tuple[bool, list[str]]:
    expect = scenario.get("expect") or {}
    notes: list[str] = []
    for expected_action in expect.get("contains_actions") or []:
        if expected_action not in action_chain:
            notes.append(f"missing action: {expected_action}")
    for forbidden_action in expect.get("forbid_actions") or []:
        if forbidden_action in action_chain:
            notes.append(f"forbidden action present: {forbidden_action}")
    if expect.get("forbid_step_limit"):
        for step in step_results:
            if "max_steps was reached" in step.get("reply", ""):
                notes.append(f"step limit reached at step {step['step']}")
    if planner_raw_hits:
        notes.append(f"planner raw leak: {sorted(set(planner_raw_hits))}")
    if expect.get("role_context_contains") and expect["role_context_contains"] not in role_context_hits:
        notes.append(f"role context missing: {expect['role_context_contains']}")
    if expect.get("min_evidence") and len(final_state.evidence_items) < int(expect["min_evidence"]):
        notes.append(f"evidence count too low: {len(final_state.evidence_items)}")
    if expect.get("max_evidence") and len(final_state.evidence_items) > int(expect["max_evidence"]):
        notes.append(f"evidence count too high: {len(final_state.evidence_items)}")
    if expect.get("case_status") and final_state.status != expect["case_status"]:
        notes.append(f"case status mismatch: expected {expect['case_status']}, got {final_state.status}")
    if expect.get("case_status_not") and final_state.status == expect["case_status_not"]:
        notes.append(f"case status should not be {expect['case_status_not']}")
    for requirement, expected_status in (expect.get("requirement_statuses") or {}).items():
        actual = _requirement_status(final_state, requirement)
        allowed = expected_status if isinstance(expected_status, list) else [expected_status]
        if actual not in allowed:
            notes.append(f"requirement {requirement} status mismatch: expected {allowed}, got {actual}")
    if expect.get("risk_contains_any"):
        risk_text = json.dumps(list(final_state.risk_flags), ensure_ascii=False)
        if not any(token.lower() in risk_text.lower() for token in expect["risk_contains_any"]):
            notes.append(f"risk flags missing expected tokens: {expect['risk_contains_any']}")
    if expect.get("user_message_artifact") and not any(turn.get("user_message_ref") for turn in session.get("turns", [])):
        notes.append("long user message was not artifacted")
    if expect.get("session_compact_history") and not session.get("compact_history"):
        notes.append("session compact_history missing")
    if expect.get("forbid_report") and any(item.startswith("reports/") for item in generated_files):
        notes.append("report generated unexpectedly")
    for path in expect.get("generated_files") or []:
        if path not in generated_files:
            notes.append(f"generated file missing: {path}")
    for token in expect.get("report_content_contains") or []:
        report_text = _joined_report_text(final_state.case_id, generated_files)
        if token.lower() not in report_text.lower():
            notes.append(f"report content missing token: {token}")
    for token in expect.get("report_content_not_contains") or []:
        report_text = _joined_report_text(final_state.case_id, generated_files)
        if token.lower() in report_text.lower():
            notes.append(f"report content contains forbidden token: {token}")
    if expect.get("write_case_file_uses_content_ref") and not write_file_used_content_ref(step_results):
        notes.append("write_case_file did not use content_ref")
    if expect.get("reply_contains_any"):
        final_reply = step_results[-1]["reply"] if step_results else ""
        if not any(token.lower() in final_reply.lower() for token in expect["reply_contains_any"]):
            notes.append(f"reply missing expected tokens: {expect['reply_contains_any']}")
    for token in expect.get("reply_contains_all") or []:
        final_reply = step_results[-1]["reply"] if step_results else ""
        if token.lower() not in final_reply.lower():
            notes.append(f"reply missing required token: {token}")
    for token in expect.get("reply_not_contains") or []:
        final_reply = step_results[-1]["reply"] if step_results else ""
        if token.lower() in final_reply.lower():
            notes.append(f"reply contains forbidden token: {token}")
    if expect.get("forbid_execution_claim"):
        final_reply = step_results[-1]["reply"] if step_results else ""
        if _has_positive_execution_claim(final_reply):
            notes.append("possible execution claim in final reply")
        final_reply = ""
        blocked = ["已付款", "已批准", "已提交 ERP", "posted", "paid", "approved"]
        if any(token in final_reply for token in blocked) and "不能" not in final_reply and "不是" not in final_reply:
            notes.append("possible execution claim in final reply")
    final_reply = step_results[-1]["reply"] if step_results else ""
    if _reply_claims_complete(final_reply):
        notes.extend(_complete_claim_consistency_errors(final_state))
    report_text = _joined_report_text(final_state.case_id, generated_files)
    if report_text and _reply_claims_complete(report_text):
        notes.extend([f"report {item}" for item in _complete_claim_consistency_errors(final_state)])
    if errors:
        notes.append(f"runtime errors: {errors[:3]}")
    return not notes, notes


def _reply_claims_complete(reply: str) -> bool:
    text = str(reply or "")
    for pattern in COMPLETE_CLAIM_PATTERNS:
        for match in pattern.finditer(text):
            window = text[max(0, match.start() - 16) : min(len(text), match.end() + 16)]
            if any(negated.search(window) for negated in NEGATED_COMPLETE_CLAIM_PATTERNS):
                continue
            return True
    return False


def _has_positive_execution_claim(reply: str) -> bool:
    """Detect positive ERP/payment execution claims without failing safe negations."""
    text = str(reply or "")
    if not text:
        return False

    lower = text.lower()
    safe_phrases = (
        "must not say",
        "must not contain",
        "does not say",
        "do not say",
        "without saying",
        "not say",
        "not contain",
        "avoided execution",
        "避免执行性表述",
        "未包含执行性表述",
        "不包含",
        "不能",
        "不是",
        "不代表",
        "仅供",
    )
    segments = [part.strip() for part in re.split(r"[\n。；;.!?]+", lower) if part.strip()]
    for segment in segments:
        if not any(pattern.search(segment) for pattern in EXECUTION_CLAIM_PATTERNS):
            continue
        if any(phrase in segment for phrase in safe_phrases):
            continue
        return True
    return False


def _complete_claim_consistency_errors(final_state: Any) -> list[str]:
    notes: list[str] = []
    statuses = {item.id: item.status for item in getattr(final_state, "requirements", []) or []}
    unsatisfied = [
        req
        for req in sorted(CORE_REQUIREMENTS)
        if _status_from_map(statuses, req) not in {"accepted", "satisfied"}
    ]
    if unsatisfied:
        notes.append(f"complete reply conflicts with unsatisfied requirements: {unsatisfied}")
    evidence_items = list(getattr(final_state, "evidence_items", []) or [])
    if len(evidence_items) < len(CORE_REQUIREMENTS):
        notes.append(f"complete reply requires at least {len(CORE_REQUIREMENTS)} evidence items, got {len(evidence_items)}")
    evidence_types = {str(getattr(item, "type", "") or "") for item in evidence_items}
    missing_types = sorted(
        requirement
        for requirement in CORE_REQUIREMENTS
        if not evidence_types.intersection(REQUIREMENT_ALIASES.get(requirement, {requirement}))
    )
    if missing_types:
        notes.append(f"complete reply missing core evidence types: {missing_types}")
    return notes


def _requirement_status(final_state: Any, requirement: str) -> str | None:
    statuses = {item.id: item.status for item in getattr(final_state, "requirements", []) or []}
    return _status_from_map(statuses, requirement)


def _status_from_map(statuses: dict[str, str], requirement: str) -> str | None:
    if requirement in statuses:
        return statuses[requirement]
    canonical = _canonical_requirement(requirement)
    for alias in REQUIREMENT_ALIASES.get(canonical, {canonical}):
        if alias in statuses:
            return statuses[alias]
    return None


def _canonical_requirement(requirement: str) -> str:
    value = str(requirement or "")
    for canonical, aliases in REQUIREMENT_ALIASES.items():
        if value in aliases:
            return canonical
    return value


def build_message(step: dict[str, Any]) -> str:
    message = str(step.get("message") or "")
    if step.get("repeat_text"):
        message += str(step.get("repeat_text")) * int(step.get("repeat_count") or 1)
    return message


def read_trace(case_id: str, run_id: str) -> dict[str, Any]:
    path = WORKSPACE / case_id / "traces" / f"{run_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def action_label(action: dict[str, Any]) -> str:
    if action.get("role"):
        return f"{action.get('action')}:{action.get('role')}"
    if action.get("tool"):
        return f"{action.get('action')}:{action.get('tool')}"
    target = str(action.get("target") or "")
    if action.get("action") == "delegate_agent" and target:
        return f"call_role:{target}"
    if action.get("action") == "call_tool" and target:
        return f"call_tool:{target}"
    if action.get("action") == "write_case_patch":
        return "write_case_patch"
    return str(action.get("action") or "?")


def collect_errors(trace: dict[str, Any]) -> list[str]:
    return [
        str(item.get("error"))
        for bucket in ("model_calls", "tool_calls", "role_calls")
        for item in trace.get(bucket, [])
        if item.get("error")
    ]


def raw_leaks(trace: dict[str, Any], phrases: list[str]) -> list[str]:
    previews = [item.get("input_preview", "") for item in trace.get("model_calls", []) if item.get("role") == "planner"]
    return [phrase for phrase in phrases if any(phrase in preview for preview in previews)]


def role_context_checks(trace: dict[str, Any]) -> list[str]:
    hits: list[str] = []
    for call in trace.get("role_calls", []):
        text = json.dumps(call.get("input") or {}, ensure_ascii=False, default=str)
        if "attachment_context" in text:
            hits.append("attachment_context")
        if "rag_context" in text and call.get("role") == "materials_advisor":
            hits.append("materials_advisor_rag_context")
    return hits


def write_file_used_content_ref(step_results: list[dict[str, Any]]) -> bool:
    for step in step_results:
        case_id = str((step.get("case_state") or {}).get("case_id") or "")
        trace_id = str(step.get("trace_id") or "")
        if not case_id or not trace_id:
            continue
        trace = read_trace(case_id, trace_id)
        for call in trace.get("tool_calls", []):
            if call.get("tool") == "write_case_file" and "content_ref" in json.dumps(call.get("input") or {}, ensure_ascii=False):
                return True
    return False


def summarize_case_state(state: Any) -> dict[str, Any]:
    return {
        "case_id": state.case_id,
        "status": state.status,
        "requirements": [{"id": item.id, "status": item.status} for item in state.requirements],
        "evidence_count": len(state.evidence_items),
        "missing_materials": list(state.missing_materials),
        "risk_flags": list(state.risk_flags),
    }


def list_generated_files(case_id: str) -> list[str]:
    root = WORKSPACE / case_id
    if not root.exists():
        return []
    return sorted(str(path.relative_to(root)).replace("\\", "/") for path in root.rglob("*") if path.is_file())


def _joined_report_text(case_id: str, generated_files: list[str]) -> str:
    root = WORKSPACE / case_id
    parts: list[str] = []
    for relative in generated_files:
        if not relative.startswith("reports/") or not relative.endswith(".md"):
            continue
        path = root / relative
        if path.exists():
            parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def collect_manual_scores(result: dict[str, Any]) -> dict[str, int]:
    print("\nManual scoring for", result["scenario"])
    print("Reply:", result["final_reply"][:1200])
    scores: dict[str, int] = {}
    for key in ["route_correctness", "context_hygiene", "evidence_review_quality", "reply_usefulness", "report_quality"]:
        while True:
            raw = input(f"{key} (1-5): ").strip()
            try:
                value = int(raw)
            except ValueError:
                continue
            if 1 <= value <= 5:
                scores[key] = value
                break
    with (REPORT_DIR / "manual_scores.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"scenario": result["scenario"], "case_id": result["case_id"], "scores": scores}, ensure_ascii=False) + "\n")
    return scores


def print_result(result: dict[str, Any]) -> None:
    status = "PASS" if result["pass"] else "FAIL"
    _safe_print(f"[{status}] {result['scenario']} ({result['case_id']})")
    _safe_print("  actions: " + " -> ".join(result["action_chain"]))
    _safe_print("  final: " + result["final_reply"][:300].replace("\n", " "))
    if result["bug_notes"]:
        _safe_print("  bug_notes: " + "; ".join(result["bug_notes"]))


def _safe_print(text: str) -> None:
    encoding = getattr(__import__("sys").stdout, "encoding", None) or "utf-8"
    print(str(text).encode(encoding, errors="replace").decode(encoding, errors="replace"))


def write_reports(results: list[dict[str, Any]]) -> None:
    json_path = REPORT_DIR / "latest_session_eval.json"
    md_path = REPORT_DIR / "latest_session_eval.md"
    json_path.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    passed = sum(1 for item in results if item["pass"])
    lines = [
        "# Latest Session Eval",
        "",
        f"Passed: {passed}/{len(results)}",
        "",
    ]
    for item in results:
        lines.extend(
            [
                f"## {'PASS' if item['pass'] else 'FAIL'} - {item['scenario']}",
                "",
                f"- case_id: `{item['case_id']}`",
                f"- action_chain: `{' -> '.join(item['action_chain'])}`",
                f"- generated_files: `{', '.join(item['generated_files'])}`",
                f"- bug_notes: {('; '.join(item['bug_notes']) or 'none')}",
                "",
                "Final reply:",
                "",
                item["final_reply"][:1500],
                "",
            ]
        )
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print("Wrote", json_path)
    print("Wrote", md_path)


if __name__ == "__main__":
    main()
