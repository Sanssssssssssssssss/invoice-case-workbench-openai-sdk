from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.evals.business.models import EvalResult, EvalSnapshot  # noqa: E402
from app.evals.business.runner import (  # noqa: E402
    DEFAULT_CASES_ROOT,
    DEFAULT_OUTPUT_ROOT,
    run_business_eval,
    score_snapshot,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="运行或重评一个 Business Eval（不支持批量）。")
    parser.add_argument("case", nargs="?", help="案例 ID 或案例目录")
    parser.add_argument("--case-dir", type=Path, help="显式指定案例目录")
    parser.add_argument("--snapshot", type=Path, help="只重评已有 snapshot.json，不调用模型")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    args = parser.parse_args()

    if args.snapshot:
        if args.case:
            parser.error("--snapshot 不能和位置参数 case 同时使用")
        paths = score_snapshot(args.snapshot, case_dir=args.case_dir)
    else:
        case_dir = args.case_dir or _resolve_case(args.case, parser)
        paths = asyncio.run(run_business_eval(case_dir, output_root=args.output_root))

    snapshot = EvalSnapshot.model_validate_json(paths.snapshot.read_text(encoding="utf-8"))
    result = EvalResult.model_validate_json(paths.score.read_text(encoding="utf-8"))
    status = "通过" if result.passed else "未通过"
    print(f"{status}：{snapshot.case_id}，得分 {result.score}/100")
    print(f"首个失败阶段：{result.first_failed_stage or '-'}")
    print(f"Snapshot：{paths.snapshot}")
    print(f"中文报告：{paths.report}")
    return 0 if result.passed else 1


def _resolve_case(value: str | None, parser: argparse.ArgumentParser) -> Path:
    if not value:
        parser.error("请提供案例 ID/目录，或使用 --snapshot")
    candidate = Path(value)
    return candidate if candidate.is_dir() else DEFAULT_CASES_ROOT / value


if __name__ == "__main__":
    raise SystemExit(main())
