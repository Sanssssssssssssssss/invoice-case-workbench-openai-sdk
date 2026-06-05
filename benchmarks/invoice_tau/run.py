from __future__ import annotations

import argparse
from pathlib import Path

from .io import discover_scenarios, new_report_dir, write_json, write_markdown
from .reporting import render_markdown, summarize_results
from .runner import InvoiceTauBenchRunner


def main() -> int:
    parser = argparse.ArgumentParser(description="Run InvoiceTauBench scenarios against the invoice workbench agent.")
    parser.add_argument("--mode", choices=["scripted", "live"], default="scripted", help="scripted uses deterministic manager/role outputs; live uses the real OpenAI manager and specialists.")
    parser.add_argument("--scenario", action="append", default=[], help="Scenario directory name under benchmarks/invoice_tau/scenarios. Repeatable.")
    parser.add_argument("--k", type=int, default=1, help="pass^k repetitions per scenario.")
    parser.add_argument("--llm-judge", action="store_true", help="Also run the native OpenAI Agents SDK judge for qualitative scoring.")
    parser.add_argument("--require-llm-judge", action="store_true", help="Combine deterministic pass with the LLM judge pass when judge is enabled.")
    parser.add_argument("--default-approval", action="store_true", help="Approve unspecified approval interrupts. Defaults to reject unspecified side effects.")
    parser.add_argument("--report-dir", default="", help="Output directory. Defaults to benchmarks/invoice_tau/reports/run_<timestamp>.")
    args = parser.parse_args()

    scenario_paths = discover_scenarios(names=args.scenario or None)
    if not scenario_paths:
        raise SystemExit("No InvoiceTauBench scenarios found.")
    report_dir = Path(args.report_dir).resolve() if args.report_dir else new_report_dir(prefix=f"{args.mode}_k{args.k}")
    runner = InvoiceTauBenchRunner(
        report_dir=report_dir,
        mode=args.mode,
        llm_judge=args.llm_judge,
        require_llm_judge=args.require_llm_judge,
        default_approval=args.default_approval,
    )
    results = []
    for scenario_path in scenario_paths:
        for index in range(1, max(1, args.k) + 1):
            result = runner.run_path(scenario_path, run_index=index)
            results.append(result)
            status = "PASS" if result.passed else "FAIL"
            print(f"[{status}] {result.scenario_id} run={index} score={result.score:.3f} wall_ms={result.metrics.get('wall_time_ms')}")
    summary = summarize_results(results, mode=args.mode, k=max(1, args.k))
    write_json(report_dir / "latest.json", summary.model_dump(mode="json"))
    write_markdown(report_dir / "latest.md", render_markdown(summary))
    print(f"\nInvoiceTauBench report: {report_dir / 'latest.md'}")
    print(f"pass@1={summary.pass_at_1:.3f} pass_all_k={summary.pass_all_k:.3f} avg_score={summary.average_score:.3f}")
    return 0 if all(item.passed for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
