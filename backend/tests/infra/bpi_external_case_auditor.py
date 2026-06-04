from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CASES_PATH = PROJECT_ROOT / "samples" / "cases" / "bpi_p2p_external_cases" / "cases.json"
DEFAULT_WORKSPACE_ROOT = PROJECT_ROOT / "workspace" / "cases"

EXPECTED_DISTRIBUTION = {
    "three_way_normal": 8,
    "invoice_before_gr": 8,
    "two_way_match": 7,
    "consignment": 7,
    "clear_invoice_trap": 5,
    "amount_variation_partial": 5,
    "reversal_cancellation_block": 5,
    "multi_turn_credibility": 5,
}

REQUIRED_GOLD_FIELDS = {
    "expected_planner_routes",
    "expected_evidence_classification",
    "expected_credibility",
    "expected_evidence_type",
    "expected_match_type",
    "expected_risks",
    "expected_missing_materials",
    "expected_case_state_after_turns",
    "report_must_include",
    "forbidden_claims",
    "scoring_layers",
}

REQUIRED_SCORING_LAYERS = {
    "planner_route",
    "evidence_review",
    "case_memory",
    "risk_explanation",
    "report_quality",
}

REQUIRED_ROUTE_MARKERS = {
    "write_case_patch",
    "call_role:materials_advisor",
    "call_role:evidence_reviewer",
    "call_role:case_patch_writer",
    "call_role:report_writer",
    "call_tool:write_case_file",
}

CHAIN_BY_LAYER = {
    "planner_route": [
        "user_turn",
        "planner_decision",
        "tool_or_role_dispatch",
    ],
    "evidence_review": [
        "user_turn",
        "planner_decision",
        "evidence_reviewer",
        "case_patch_writer",
    ],
    "case_memory": [
        "evidence_reviewer",
        "case_patch_writer",
        "write_case_patch",
        "case_state",
    ],
    "risk_explanation": [
        "process_log_facts",
        "evidence_reviewer",
        "case_state_risk_flags",
        "report_writer",
    ],
    "report_quality": [
        "case_state",
        "report_writer",
        "write_case_file",
        "manager_memo",
    ],
}


@dataclass
class AuditFinding:
    case_id: str
    severity: str
    layer: str
    title: str
    expected: Any = ""
    observed: Any = ""
    turn_index: int | None = None
    problem_chain: list[str] = field(default_factory=list)
    next_action: str = ""


@dataclass
class AuditPass:
    status: str
    cases_checked: int
    mode: str


class StrictBpiExternalCaseAuditor:
    """Fail-fast auditor for BPI external cases and observed agent runs.

    The auditor is intentionally strict. It is meant to find the first visible
    system-chain problem, not to compute an average score after many failures.
    """

    def audit_contract(self, cases: list[dict[str, Any]], *, case_id: str | None = None, limit: int | None = None) -> AuditFinding | AuditPass:
        selected = _select_cases(cases, case_id=case_id, limit=limit)
        if case_id is None and limit is None:
            suite_finding = self._audit_contract_suite(cases)
            if suite_finding:
                return suite_finding
        checked = 0
        for case in selected:
            checked += 1
            finding = self._audit_contract_case(case)
            if finding:
                return finding
        return AuditPass(status="pass", cases_checked=checked, mode="contract")

    def audit_run(
        self,
        cases: list[dict[str, Any]],
        observed_by_case: dict[str, dict[str, Any]],
        *,
        case_id: str | None = None,
        limit: int | None = None,
    ) -> AuditFinding | AuditPass:
        selected = _select_cases(cases, case_id=case_id, limit=limit)
        checked = 0
        for case in selected:
            checked += 1
            observed = observed_by_case.get(case["case_id"])
            if not observed:
                return finding(
                    case,
                    "critical",
                    "planner_route",
                    "No observed run found for case",
                    expected="a trace/workspace output for this case",
                    observed="missing",
                    next_action="Run exactly this case once, then audit its trace before running more cases.",
                )
            first_issue = (
                self._audit_planner_route(case, observed)
                or self._audit_evidence_review(case, observed)
                or self._audit_case_memory(case, observed)
                or self._audit_risk_explanation(case, observed)
                or self._audit_report_quality(case, observed)
            )
            if first_issue:
                return first_issue
        return AuditPass(status="pass", cases_checked=checked, mode="run")

    def _audit_contract_suite(self, cases: list[dict[str, Any]]) -> AuditFinding | None:
        if len(cases) != 50:
            return AuditFinding(
                case_id="suite",
                severity="critical",
                layer="case_contract",
                title="Wrong number of external cases",
                expected=50,
                observed=len(cases),
                problem_chain=["case_fixture", "auditor_contract", "future_run_selection"],
                next_action="Fix cases.json generation before evaluating agent behavior.",
            )
        counts: dict[str, int] = {}
        for case in cases:
            counts[str(case.get("category") or "")] = counts.get(str(case.get("category") or ""), 0) + 1
        if counts != EXPECTED_DISTRIBUTION:
            return AuditFinding(
                case_id="suite",
                severity="critical",
                layer="case_contract",
                title="Category distribution drifted",
                expected=EXPECTED_DISTRIBUTION,
                observed=counts,
                problem_chain=["case_fixture", "category_coverage", "risk_coverage"],
                next_action="Regenerate or manually fix the suite distribution.",
            )
        ids = [str(case.get("case_id") or "") for case in cases]
        if len(ids) != len(set(ids)):
            return AuditFinding(
                case_id="suite",
                severity="critical",
                layer="case_contract",
                title="Duplicate case_id in external suite",
                expected="50 unique case IDs",
                observed=ids,
                problem_chain=["case_fixture", "case_memory", "workspace_isolation"],
                next_action="Make case IDs unique before any run-level audit.",
            )
        return None

    def _audit_contract_case(self, case: dict[str, Any]) -> AuditFinding | None:
        case_id = str(case.get("case_id") or "")
        if not case_id:
            return raw_finding("unknown", "critical", "case_contract", "Case is missing case_id")
        gold = case.get("gold") or {}
        missing_gold = sorted(REQUIRED_GOLD_FIELDS - set(gold))
        if missing_gold:
            return finding(
                case,
                "critical",
                "case_contract",
                "Gold labels are incomplete",
                expected=sorted(REQUIRED_GOLD_FIELDS),
                observed={"missing": missing_gold},
                next_action="Add the missing gold fields before using this case in run audit.",
            )
        if set(gold.get("scoring_layers") or []) != REQUIRED_SCORING_LAYERS:
            return finding(
                case,
                "critical",
                "case_contract",
                "Scoring layers are not the strict five-layer contract",
                expected=sorted(REQUIRED_SCORING_LAYERS),
                observed=gold.get("scoring_layers"),
                next_action="Use exactly planner_route, evidence_review, case_memory, risk_explanation, report_quality.",
            )
        routes = set(gold.get("expected_planner_routes") or [])
        missing_routes = sorted(REQUIRED_ROUTE_MARKERS - routes)
        if missing_routes:
            return finding(
                case,
                "critical",
                "planner_route",
                "Gold route markers are incomplete",
                expected=sorted(REQUIRED_ROUTE_MARKERS),
                observed={"missing": missing_routes, "routes": sorted(routes)},
                next_action="Make the expected planner chain explicit in gold labels.",
            )
        process_packets = [packet for packet in case.get("evidence_packets", []) if packet.get("type") == "process_log"]
        if not process_packets:
            return finding(
                case,
                "critical",
                "evidence_review",
                "No BPI process_log evidence packet",
                expected="at least one process_log packet",
                observed=case.get("evidence_packets"),
                next_action="Add BPI/event-log evidence as supporting process evidence.",
            )
        for packet in process_packets:
            source = str(packet.get("source") or "")
            notes = " ".join((packet.get("content") or {}).get("notes") or [])
            if "no raw CSV row included" not in source or "not an action executed by this agent" not in notes:
                return finding(
                    case,
                    "critical",
                    "evidence_review",
                    "BPI process evidence boundary is unclear",
                    expected="no raw CSV, supporting process evidence, Clear Invoice not agent action",
                    observed={"source": source, "notes": notes},
                    next_action="Tighten the evidence packet boundary text.",
                )
        forbidden = " ".join(str(item) for item in gold.get("forbidden_claims") or []).lower()
        for required in ("erp action executed", "invoice paid by this agent", "bpi clear invoice"):
            if required not in forbidden:
                return finding(
                    case,
                    "critical",
                    "report_quality",
                    "Forbidden claim coverage is incomplete",
                    expected=required,
                    observed=gold.get("forbidden_claims"),
                    next_action="Add explicit forbidden claims for no-ERP-action and Clear Invoice overclaim.",
                )
        return None

    def _audit_planner_route(self, case: dict[str, Any], observed: dict[str, Any]) -> AuditFinding | None:
        turns = _observed_turns(observed)
        expected_turns = case.get("turns") or []
        if len(turns) < len(expected_turns):
            return finding(
                case,
                "critical",
                "planner_route",
                "Observed run has fewer turns than the case contract",
                expected=len(expected_turns),
                observed=len(turns),
                next_action="Run the case one turn at a time and preserve every turn trace.",
            )
        for index, expected_turn in enumerate(expected_turns):
            expected_tokens = _expected_tokens(str(expected_turn.get("expected_planner_action") or ""))
            actual_tokens = _actual_route_tokens(turns[index])
            if not _is_subsequence(expected_tokens, actual_tokens):
                return finding(
                    case,
                    "critical",
                    "planner_route",
                    "Planner selected the wrong next-step chain",
                    expected=expected_tokens,
                    observed=actual_tokens,
                    turn_index=index + 1,
                    next_action="Inspect the planner prompt/observations for this turn before running more cases.",
                )
        return None

    def _audit_evidence_review(self, case: dict[str, Any], observed: dict[str, Any]) -> AuditFinding | None:
        evidence_turn_index = _first_evidence_turn_index(case)
        if evidence_turn_index is None:
            return None
        turn = _observed_turns(observed)[evidence_turn_index]
        actual_tokens = _actual_route_tokens(turn)
        for required in ("call_role:evidence_reviewer", "call_role:case_patch_writer", "write_case_patch"):
            if required not in actual_tokens:
                return finding(
                    case,
                    "critical",
                    "evidence_review",
                    "Evidence submission did not pass through the full review-to-patch chain",
                    expected=required,
                    observed=actual_tokens,
                    turn_index=evidence_turn_index + 1,
                    next_action="Do not score the final memo until evidence review and patch persistence are repaired.",
                )
        state = _case_state_at_or_after(observed, evidence_turn_index)
        evidence_items = _evidence_items(state)
        expected_classification = (case.get("gold") or {}).get("expected_evidence_classification") or {}
        for evidence_id, expected_type in expected_classification.items():
            item = _find_evidence_item(evidence_items, evidence_id)
            if not item:
                return finding(
                    case,
                    "major",
                    "evidence_review",
                    "Evidence packet ID was not preserved into case state",
                    expected=evidence_id,
                    observed=[item.get("id") for item in evidence_items],
                    turn_index=evidence_turn_index + 1,
                    next_action="Make the evidence reviewer/case patch writer carry evidence_id through suggested_patch.",
                )
            observed_type = _normalize_evidence_type(str(item.get("type") or ""))
            if observed_type != _normalize_evidence_type(str(expected_type)):
                return finding(
                    case,
                    "major",
                    "evidence_review",
                    "Evidence was classified as the wrong type",
                    expected={evidence_id: expected_type},
                    observed={evidence_id: item.get("type")},
                    turn_index=evidence_turn_index + 1,
                    next_action="Fix evidence type taxonomy before judging downstream report quality.",
                )
            expected_credibility = ((case.get("gold") or {}).get("expected_credibility") or {}).get(evidence_id)
            if expected_credibility and not _credibility_matches(str(expected_credibility), str(item.get("credibility") or "")):
                return finding(
                    case,
                    "major",
                    "evidence_review",
                    "Evidence credibility did not match the gold label",
                    expected={evidence_id: expected_credibility},
                    observed={evidence_id: item.get("credibility")},
                    turn_index=evidence_turn_index + 1,
                    next_action="Tighten reviewer credibility rules for process logs, user statements, and text-only invoices.",
                )
        return None

    def _audit_case_memory(self, case: dict[str, Any], observed: dict[str, Any]) -> AuditFinding | None:
        state = _final_case_state(observed)
        if not state:
            return finding(
                case,
                "critical",
                "case_memory",
                "No final case_state was captured",
                expected="case_state after final turn",
                observed="missing",
                next_action="Persist and export case_state with every audited run.",
            )
        gold = case.get("gold") or {}
        expected_state = gold.get("expected_case_state_after_turns") or {}
        expected_status = expected_state.get("status")
        if expected_status and str(state.get("status") or "") != expected_status:
            return finding(
                case,
                "major",
                "case_memory",
                "Case state ended in the wrong status",
                expected=expected_status,
                observed=state.get("status"),
                next_action="Review status transitions after evidence and report turns.",
            )
        risk_flags = set(str(item) for item in state.get("risk_flags") or [])
        missing_risks = [risk for risk in gold.get("expected_risks") or [] if risk not in risk_flags]
        if missing_risks:
            return finding(
                case,
                "major",
                "case_memory",
                "Expected risk flags were not persisted",
                expected=gold.get("expected_risks"),
                observed=sorted(risk_flags),
                next_action="Trace evidence_reviewer.suggested_patch -> case_patch_writer -> write_case_patch.",
            )
        missing_materials = set(str(item) for item in state.get("missing_materials") or [])
        missing_expected = [item for item in gold.get("expected_missing_materials") or [] if item not in missing_materials]
        if missing_expected:
            return finding(
                case,
                "major",
                "case_memory",
                "Expected missing materials were not preserved",
                expected=gold.get("expected_missing_materials"),
                observed=sorted(missing_materials),
                next_action="Make missing materials additive and avoid overwriting earlier gaps.",
            )
        return None

    def _audit_risk_explanation(self, case: dict[str, Any], observed: dict[str, Any]) -> AuditFinding | None:
        combined_text = _combined_text(observed)
        for phrase in (case.get("gold") or {}).get("report_must_include") or []:
            if str(phrase).lower() not in combined_text:
                return finding(
                    case,
                    "major",
                    "risk_explanation",
                    "Required risk/process explanation is missing from user-visible outputs",
                    expected=phrase,
                    observed=_preview(combined_text),
                    next_action="Fix the link from process evidence facts to reviewer notes and report instructions.",
                )
        return None

    def _audit_report_quality(self, case: dict[str, Any], observed: dict[str, Any]) -> AuditFinding | None:
        report = _report_markdown(observed)
        if not report.strip():
            return finding(
                case,
                "critical",
                "report_quality",
                "Final report was not generated or captured",
                expected="reports/final_report.md content",
                observed="missing",
                next_action="Stop here: report_writer/write_case_file must be repaired before broader evaluation.",
            )
        lowered = report.lower()
        for forbidden in (case.get("gold") or {}).get("forbidden_claims") or []:
            if str(forbidden).lower() in lowered:
                return finding(
                    case,
                    "critical",
                    "report_quality",
                    "Manager memo contains a forbidden overclaim",
                    expected=f"must not contain: {forbidden}",
                    observed=_preview(report),
                    next_action="Tighten no-action wording guard and report prompt before any further run.",
                )
        if "#" not in report[:120]:
            return finding(
                case,
                "minor",
                "report_quality",
                "Manager memo is not a structured Markdown report",
                expected="Markdown title near the top",
                observed=_preview(report),
                next_action="Adjust report_writer output format.",
            )
        return None


def finding(
    case: dict[str, Any],
    severity: str,
    layer: str,
    title: str,
    *,
    expected: Any = "",
    observed: Any = "",
    turn_index: int | None = None,
    next_action: str = "",
) -> AuditFinding:
    return AuditFinding(
        case_id=str(case.get("case_id") or "unknown"),
        severity=severity,
        layer=layer,
        title=title,
        expected=expected,
        observed=observed,
        turn_index=turn_index,
        problem_chain=list(CHAIN_BY_LAYER.get(layer, [layer])),
        next_action=next_action,
    )


def raw_finding(case_id: str, severity: str, layer: str, title: str) -> AuditFinding:
    return AuditFinding(
        case_id=case_id,
        severity=severity,
        layer=layer,
        title=title,
        problem_chain=list(CHAIN_BY_LAYER.get(layer, [layer])),
    )


def load_cases(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("cases"), list):
        return list(data["cases"])
    if isinstance(data, list):
        return list(data)
    raise ValueError(f"Unsupported cases file shape: {path}")


def load_observed(path: Path | None = None, workspace_root: Path | None = None, cases: list[dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    if path:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("cases"), list):
            return {str(item["case_id"]): item for item in data["cases"]}
        if isinstance(data, list):
            return {str(item["case_id"]): item for item in data}
        if isinstance(data, dict) and "case_id" in data:
            return {str(data["case_id"]): data}
        if isinstance(data, dict):
            return {str(key): value for key, value in data.items() if isinstance(value, dict)}
        raise ValueError(f"Unsupported observed file shape: {path}")
    if workspace_root:
        if cases is None:
            raise ValueError("cases are required when loading observed data from a workspace root")
        return {case["case_id"]: _load_workspace_case(workspace_root, case["case_id"]) for case in cases if (workspace_root / case["case_id"]).exists()}
    return {}


def _load_workspace_case(workspace_root: Path, case_id: str) -> dict[str, Any]:
    root = workspace_root / case_id
    traces = []
    for path in sorted((root / "traces").glob("run_*.json")):
        try:
            traces.append(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    traces.sort(key=lambda item: str(item.get("started_at") or ""))
    final_state: dict[str, Any] = {}
    state_path = root / "case_state.json"
    if state_path.exists():
        final_state = json.loads(state_path.read_text(encoding="utf-8"))
    turns = []
    for trace in traces:
        turns.append(
            {
                "trace": trace,
                "reply": trace.get("final_answer", ""),
                "case_state": final_state,
            }
        )
    report_path = root / "reports" / "final_report.md"
    report = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
    return {"case_id": case_id, "turns": turns, "case_state": final_state, "report_markdown": report}


def _select_cases(cases: list[dict[str, Any]], *, case_id: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    selected = [case for case in cases if case_id is None or case.get("case_id") == case_id]
    if case_id and not selected:
        raise ValueError(f"case_id not found: {case_id}")
    if limit is not None:
        selected = selected[: max(0, int(limit))]
    return selected


def _observed_turns(observed: dict[str, Any]) -> list[dict[str, Any]]:
    turns = observed.get("turns")
    if isinstance(turns, list):
        return [turn for turn in turns if isinstance(turn, dict)]
    trace = observed.get("trace")
    if isinstance(trace, dict):
        return [{"trace": trace, "reply": observed.get("reply", ""), "case_state": observed.get("case_state", {})}]
    return []


def _expected_tokens(value: str) -> list[str]:
    return [part.strip() for part in value.split("->") if part.strip()]


def _actual_route_tokens(turn: dict[str, Any]) -> list[str]:
    trace = turn.get("trace") if isinstance(turn.get("trace"), dict) else turn
    actions = trace.get("planner_actions") or []
    tokens = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        action_type = str(action.get("action") or "")
        if action_type == "call_tool":
            tokens.append(f"call_tool:{action.get('tool')}")
        elif action_type == "call_role":
            tokens.append(f"call_role:{action.get('role')}")
        elif action_type == "write_case_patch":
            tokens.append("write_case_patch")
        elif action_type == "final_answer":
            tokens.append("final_answer")
        else:
            tokens.append(action_type)
    return tokens


def _is_subsequence(expected: list[str], actual: list[str]) -> bool:
    cursor = 0
    for token in actual:
        if cursor < len(expected) and token == expected[cursor]:
            cursor += 1
    return cursor == len(expected)


def _first_evidence_turn_index(case: dict[str, Any]) -> int | None:
    for index, turn in enumerate(case.get("turns") or []):
        if turn.get("evidence_packet_ids"):
            return index
    return None


def _case_state_at_or_after(observed: dict[str, Any], turn_index: int) -> dict[str, Any]:
    turns = _observed_turns(observed)
    for turn in turns[turn_index:]:
        state = turn.get("case_state")
        if isinstance(state, dict) and state:
            return state
    return _final_case_state(observed)


def _final_case_state(observed: dict[str, Any]) -> dict[str, Any]:
    state = observed.get("case_state")
    if isinstance(state, dict) and state:
        return state
    turns = _observed_turns(observed)
    for turn in reversed(turns):
        turn_state = turn.get("case_state")
        if isinstance(turn_state, dict) and turn_state:
            return turn_state
    return {}


def _evidence_items(state: dict[str, Any]) -> list[dict[str, Any]]:
    items = state.get("evidence_items") or []
    return [item for item in items if isinstance(item, dict)]


def _find_evidence_item(items: list[dict[str, Any]], evidence_id: str) -> dict[str, Any] | None:
    for item in items:
        if str(item.get("id") or "") == evidence_id:
            return item
    return None


def _normalize_evidence_type(value: str) -> str:
    normalized = value.strip().lower()
    mapping = {
        "invoice_text": "invoice",
        "purchase_order_text": "purchase_order",
        "goods_receipt_text": "goods_receipt",
        "duplicate_payment_check": "duplicate_payment_check",
        "duplicate_check": "duplicate_payment_check",
    }
    return mapping.get(normalized, normalized)


def _credibility_matches(expected: str, observed: str) -> bool:
    expected = expected.strip().lower()
    observed = observed.strip().lower()
    if expected == "low_or_medium":
        return observed in {"low", "medium", "low_or_medium"}
    return expected == observed


def _combined_text(observed: dict[str, Any]) -> str:
    parts = [str(observed.get("reply") or ""), str(observed.get("report_markdown") or "")]
    state = _final_case_state(observed)
    parts.append(json.dumps(state, ensure_ascii=False, default=str))
    for turn in _observed_turns(observed):
        parts.append(str(turn.get("reply") or ""))
        trace = turn.get("trace") if isinstance(turn.get("trace"), dict) else turn
        for key in ("role_calls", "tool_calls", "planner_actions"):
            parts.append(json.dumps(trace.get(key) or [], ensure_ascii=False, default=str))
    return "\n".join(parts).lower()


def _report_markdown(observed: dict[str, Any]) -> str:
    report = observed.get("report_markdown")
    if isinstance(report, str):
        return report
    reports = observed.get("reports")
    if isinstance(reports, dict):
        for key in ("final_report.md", "reports/final_report.md", "final_report"):
            value = reports.get(key)
            if isinstance(value, str):
                return value
    return ""


def _preview(value: Any, limit: int = 500) -> str:
    text = str(value)
    return text[:limit] + ("..." if len(text) > limit else "")


def _print_result(result: AuditFinding | AuditPass, *, as_json: bool) -> None:
    payload = asdict(result)
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return
    if isinstance(result, AuditPass):
        print(f"PASS mode={result.mode} cases_checked={result.cases_checked}")
        return
    print(f"[{result.severity.upper()}] {result.case_id} {result.layer}: {result.title}")
    if result.turn_index is not None:
        print(f"turn_index: {result.turn_index}")
    print(f"expected: {json.dumps(result.expected, ensure_ascii=False, default=str)}")
    print(f"observed: {json.dumps(result.observed, ensure_ascii=False, default=str)}")
    print("problem_chain: " + " -> ".join(result.problem_chain))
    if result.next_action:
        print(f"next_action: {result.next_action}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail-fast strict auditor for BPI external invoice-payment cases.")
    parser.add_argument("mode", choices=["contract", "run"])
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)
    parser.add_argument("--observed", type=Path, help="Observed run JSON. Required for run mode unless --workspace-root is used.")
    parser.add_argument("--workspace-root", type=Path, help="Workspace/cases root to load observed traces from.")
    parser.add_argument("--case-id", help="Audit one case only.")
    parser.add_argument("--limit", type=int, help="Audit only the first N selected cases.")
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    args = parser.parse_args(argv)

    try:
        cases = load_cases(args.cases)
        auditor = StrictBpiExternalCaseAuditor()
        if args.mode == "contract":
            result = auditor.audit_contract(cases, case_id=args.case_id, limit=args.limit)
        else:
            observed = load_observed(args.observed, args.workspace_root, cases)
            result = auditor.audit_run(cases, observed, case_id=args.case_id, limit=args.limit)
        _print_result(result, as_json=args.json)
        return 2 if isinstance(result, AuditFinding) else 0
    except Exception as exc:
        error = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        if args.json:
            print(json.dumps(error, ensure_ascii=False, indent=2))
        else:
            print(error["error"], file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
