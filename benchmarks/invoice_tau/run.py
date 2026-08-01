from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

from .io import discover_scenarios, new_report_dir, write_json, write_markdown
from .profiles import get_profile, profile_names
from .reporting import render_markdown, summarize_results
from .runner import InvoiceTauBenchRunner


def main() -> int:
    _configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Run InvoiceTauBench scenarios against the invoice workbench agent.")
    parser.add_argument("--profile", choices=profile_names(), default="", help="Named benchmark profile. Live profiles call the real LLM by default.")
    parser.add_argument("--mode", choices=["scripted", "live"], default="", help="Override profile mode. scripted uses deterministic outputs; live uses the real OpenAI manager and specialists.")
    parser.add_argument("--scenario", action="append", default=[], help="Scenario directory name under benchmarks/invoice_tau/scenarios. Repeatable.")
    parser.add_argument("--scenario-root", default="", help="Directory containing scenario folders. Defaults to benchmarks/invoice_tau/scenarios.")
    parser.add_argument("--k", type=int, default=1, help="pass^k repetitions per scenario.")
    parser.add_argument("--llm-judge", action="store_true", help="Also run the native OpenAI Agents SDK judge for qualitative scoring.")
    parser.add_argument("--no-llm-judge", action="store_true", help="Disable the profile's default LLM judge to save tokens.")
    parser.add_argument("--require-llm-judge", action="store_true", help="Combine deterministic pass with the LLM judge pass when judge is enabled.")
    parser.add_argument("--default-approval", action="store_true", help="Approve unspecified approval interrupts. Defaults to reject unspecified side effects.")
    parser.add_argument("--report-dir", default="", help="Output directory. Defaults to benchmarks/invoice_tau/reports/run_<timestamp>.")
    args = parser.parse_args()

    profile = get_profile(args.profile) if args.profile else None
    mode = args.mode or (profile.mode if profile else "scripted")
    scenario_names = args.scenario or (list(profile.scenarios) if profile and profile.scenarios else None)
    scenario_root = Path(args.scenario_root).resolve() if args.scenario_root else None
    scenario_paths = discover_scenarios(root=scenario_root, names=scenario_names) if scenario_root else discover_scenarios(names=scenario_names)
    if not scenario_paths:
        raise SystemExit("No InvoiceTauBench scenarios found.")
    llm_judge = bool(args.llm_judge or (profile.llm_judge if profile else False))
    if args.no_llm_judge:
        llm_judge = False
    prefix = f"{args.profile or mode}_k{args.k}"
    report_dir = Path(args.report_dir).resolve() if args.report_dir else new_report_dir(prefix=prefix)
    runner = InvoiceTauBenchRunner(
        report_dir=report_dir,
        mode=mode,
        llm_judge=llm_judge,
        require_llm_judge=args.require_llm_judge,
        default_approval=args.default_approval,
    )
    results = []
    for scenario_path in scenario_paths:
        for index in range(1, max(1, args.k) + 1):
            result = runner.run_path(scenario_path, run_index=index)
            results.append(result)
            status = "PASS" if result.passed else "FAIL"
            judge = ""
            if result.llm_judge.enabled:
                judge = f" judge={result.llm_judge.score:.3f}" if not result.llm_judge.error else f" judge_error={result.llm_judge.error[:80]}"
            print(f"[{status}] {result.scenario_id} run={index} score={result.score:.3f}{judge} wall_ms={result.metrics.get('wall_time_ms')}")
    summary = summarize_results(results, mode=mode, k=max(1, args.k), profile=args.profile)
    write_json(report_dir / "latest.json", summary.model_dump(mode="json"))
    write_markdown(report_dir / "latest.md", render_markdown(summary))
    print(f"\nInvoiceTauBench report: {report_dir / 'latest.md'}")
    print(
        "contract_pass@1="
        f"{summary.contract_pass_at_1:.3f} pass_all_k={summary.pass_all_k:.3f} "
        f"deterministic_score={summary.deterministic_score:.3f} "
        f"llm_quality_score={summary.llm_quality_score if summary.llm_quality_score is not None else 'n/a'}"
    )
    judge_errors = any(item.llm_judge.enabled and item.llm_judge.error for item in results)
    return 0 if all(item.passed for item in results) and not judge_errors else 1


def _configure_utf8_stdio() -> None:
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    raise SystemExit(main())
