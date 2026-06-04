from __future__ import annotations

import shutil
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import get_settings
from app.runtime.turn_runner import AgentRuntime
from app.state.schemas import AgentTurnRequest


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    get_settings.cache_clear()
    runtime = AgentRuntime()
    case_id = "case_smoke"
    case_dir = Path("workspace") / "cases" / case_id
    if case_dir.exists():
        shutil.rmtree(case_dir)
    messages = [
        "帮我新建一个发票付款审查 case。",
        "我现在需要准备什么？",
        "这是发票 INV-001，金额 10000 CNY，供应商 ABC。",
        "生成报告。",
    ]
    for index, message in enumerate(messages, start=1):
        response = runtime.run_turn(AgentTurnRequest(case_id=case_id, message=message))
        print(f"\n[{index}] {message}")
        print(response.reply)
        print(f"status={response.case_state.status} evidence={len(response.case_state.evidence_items)} steps={response.trace.get('step_count')}")
    print(f"\ncase_workspace={case_dir.resolve()}")


if __name__ == "__main__":
    main()
