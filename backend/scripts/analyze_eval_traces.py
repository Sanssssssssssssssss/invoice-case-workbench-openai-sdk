from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
REPORT_DIR = ROOT / "reports" / "evals"
WORKSPACE = ROOT / "workspace" / "cases"

ERP_HINT_PATTERNS = (
    re.compile(r"提交\s*(?:至|到)?\s*ERP", re.I),
    re.compile(r"推进付款审批"),
    re.compile(r"可直接(?:付款|审批|提交)"),
    re.compile(r"\bsubmit\s+(?:to\s+)?ERP\b", re.I),
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--eval-json", default=str(REPORT_DIR / "latest_session_eval.json"))
    parser.add_argument("--output", default=str(REPORT_DIR / "trace_role_boundary_analysis.md"))
    args = parser.parse_args()

    results = json.loads(Path(args.eval_json).read_text(encoding="utf-8"))
    lines = [
        "# Trace Role Boundary Analysis",
        "",
        "This report is generated from real run traces. It is intended for prompt boundary tuning, not as an automated pass/fail substitute.",
        "",
        f"- eval_json: `{Path(args.eval_json).as_posix()}`",
        f"- scenarios: `{len(results)}`",
        "",
    ]
    for result in results:
        lines.extend(render_result(result))
    Path(args.output).write_text("\n".join(lines), encoding="utf-8")
    print("Wrote", args.output)


def render_result(result: dict[str, Any]) -> list[str]:
    case_id = str(result.get("case_id") or "")
    lines = [
        f"## {'PASS' if result.get('pass') else 'FAIL'} - {result.get('scenario')}",
        "",
        f"- case_id: `{case_id}`",
        f"- final_status: `{(result.get('case_state_delta') or {}).get('status')}`",
        f"- evidence_count: `{(result.get('case_state_delta') or {}).get('evidence_count')}`",
        f"- action_chain: `{' -> '.join(result.get('action_chain') or [])}`",
        f"- bug_notes: `{'; '.join(result.get('bug_notes') or []) or 'none'}`",
        "",
    ]
    for step in result.get("steps") or []:
        trace = read_trace(case_id, str(step.get("trace_id") or ""))
        lines.extend(render_step(step, trace))
    lines.append("")
    return lines


def render_step(step: dict[str, Any], trace: dict[str, Any]) -> list[str]:
    lines = [
        f"### Step {step.get('step')}: {preview(step.get('message'), 140)}",
        "",
        f"- trace: `{trace.get('run_id', step.get('trace_id'))}`",
        f"- actions: `{' -> '.join(step.get('action_chain') or [])}`",
        f"- final_reply_preview: {preview(step.get('reply'), 260)}",
    ]
    planner_routes = [
        str(action.get("plan_progress") or "")
        for action in trace.get("planner_actions") or []
        if action.get("plan_progress")
    ]
    if planner_routes:
        lines.append(f"- planner_routes: `{'; '.join(planner_routes)}`")
    boundary_hits = boundary_hits_in_text(step.get("reply", ""))
    if boundary_hits:
        lines.append(f"- boundary_hits_in_reply: `{', '.join(boundary_hits)}`")
    lines.append("")
    role_calls = trace.get("role_calls") or []
    if role_calls:
        lines.append("Role outputs:")
        for call in role_calls:
            lines.append(
                f"- `{call.get('role')}` error=`{call.get('error') or ''}` result=`{preview(call.get('result_preview'), 420)}`"
            )
        lines.append("")
    tool_calls = trace.get("tool_calls") or []
    if tool_calls:
        lines.append("Tool outputs:")
        for call in tool_calls:
            lines.append(
                f"- `{call.get('tool')}` error=`{call.get('error') or ''}` result=`{preview(call.get('result_preview'), 260)}`"
            )
        lines.append("")
    return lines


def read_trace(case_id: str, run_id: str) -> dict[str, Any]:
    if not case_id or not run_id:
        return {}
    path = WORKSPACE / case_id / "traces" / f"{run_id}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def preview(value: Any, max_chars: int) -> str:
    text = str(value or "").replace("\n", " ").strip()
    return text[:max_chars] + ("..." if len(text) > max_chars else "")


def boundary_hits_in_text(text: Any) -> list[str]:
    value = str(text or "")
    return [pattern.pattern for pattern in ERP_HINT_PATTERNS if pattern.search(value)]


if __name__ == "__main__":
    main()
