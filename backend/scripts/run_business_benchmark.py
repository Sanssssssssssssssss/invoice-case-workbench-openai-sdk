from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.evals.business.benchmark import (  # noqa: E402
    discover_business_cases,
    rescore_business_benchmark,
    run_business_benchmark,
)
from app.evals.business.runner import DEFAULT_CASES_ROOT  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="顺序运行或零 API 重评 Business Benchmark。")
    parser.add_argument("cases", nargs="*", help="案例 ID 或案例目录；真实运行至少提供一个，或使用 --all")
    parser.add_argument("--all", action="store_true", help="发现并顺序运行 cases-root 下的全部案例")
    parser.add_argument("--suite", action="append", default=[], help="--all 时筛选 suite，可重复")
    parser.add_argument("--cases-root", type=Path, default=DEFAULT_CASES_ROOT)
    parser.add_argument("--snapshot-root", type=Path, help="批量重评目录下的 snapshot.json，不调用模型")
    parser.add_argument("--output-dir", type=Path, help="汇总输出目录；live 模式要求该目录尚不存在")
    args = parser.parse_args()

    if args.snapshot_root:
        if args.cases or args.all or args.suite:
            parser.error("--snapshot-root 不能与 live 案例参数同时使用")
        paths = rescore_business_benchmark(
            args.snapshot_root,
            cases_root=args.cases_root,
            output_dir=args.output_dir,
        )
    else:
        if args.all and args.cases:
            parser.error("--all 不能与显式 cases 同时使用")
        if args.suite and not args.all:
            parser.error("--suite 仅与 --all 一起使用")
        case_dirs = (
            discover_business_cases(args.cases_root, suites=args.suite)
            if args.all
            else [_resolve_case(value, args.cases_root) for value in args.cases]
        )
        if not case_dirs:
            parser.error("请提供案例 ID/目录，或使用 --all/--snapshot-root")
        paths = asyncio.run(
            run_business_benchmark(case_dirs, output_dir=args.output_dir)
        )

    summary = json.loads(paths.summary.read_text(encoding="utf-8"))
    print(
        f"严格结果：{'通过' if summary['strict_pass'] else '未通过'}；"
        f"运行 {summary['run_count']}；平均分 {summary['score_mean']:.2f}；最低分 {summary['score_min']:.2f}"
    )
    print(f"结构化结果：{paths.summary}")
    print(f"中文报告：{paths.report}")
    return 0 if summary["strict_pass"] else 1


def _resolve_case(value: str, cases_root: Path) -> Path:
    candidate = Path(value)
    return candidate if candidate.is_dir() else cases_root / value


if __name__ == "__main__":
    raise SystemExit(main())
