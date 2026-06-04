from __future__ import annotations

import json
import argparse
import shutil
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import get_settings
from app.runtime.turn_runner import AgentRuntime
from app.state.schemas import AgentTurnRequest, Attachment


SAMPLE_ROOT = PROJECT_ROOT / "samples" / "cases" / "invoice_payment_case_001"
CASE_ID = "case_sample_001"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run sample invoice case turns.")
    parser.add_argument("--full", action="store_true", help="Submit all sample evidence files instead of the fast smoke subset.")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

    get_settings.cache_clear()
    workspace = PROJECT_ROOT / "workspace" / "cases" / CASE_ID
    if workspace.exists():
        shutil.rmtree(workspace)

    runtime = AgentRuntime()
    turns = _load_turns(full=args.full)
    for index, turn in enumerate(turns, start=1):
        request = _turn_request(turn)
        message = request.message
        print(f"\n[{index}] {message[:120]}")
        response = runtime.run_turn(request)
        print(response.reply)
        print(
            "status={status} evidence={evidence} steps={steps}".format(
                status=response.case_state.status,
                evidence=len(response.case_state.evidence_items),
                steps=response.trace.get("step_count"),
            )
        )
    print(f"\ncase_workspace={workspace.resolve()}")


def _load_turns(*, full: bool) -> list[dict[str, str]]:
    turns: list[dict[str, str]] = []
    for line in (SAMPLE_ROOT / "turns.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        turns.append(json.loads(line))
    if full:
        return turns
    return [
        turns[0],
        turns[1],
        turns[2],
        turns[-1],
    ]


def _turn_request(turn: dict[str, str]) -> AgentTurnRequest:
    if "message" in turn:
        return AgentTurnRequest(case_id=CASE_ID, message=turn["message"])
    rel = turn["file"]
    path = SAMPLE_ROOT / rel
    return AgentTurnRequest(
        case_id=CASE_ID,
        message=f"请审查我提交的材料文件：{rel}",
        attachments=[Attachment(name=Path(rel).name, path=str(path.resolve()), content_type="text/markdown")],
    )


if __name__ == "__main__":
    main()
