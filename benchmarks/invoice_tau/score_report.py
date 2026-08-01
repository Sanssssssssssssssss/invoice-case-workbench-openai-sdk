from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

from .io import SCENARIOS_ROOT, load_scenario, write_json, write_markdown
from .judge import run_llm_judge
from .models import BenchmarkSummary, ScenarioRunResult
from .reporting import render_markdown, summarize_results
from .verifiers import score_checks, verify_run


def main() -> int:
    _configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Run only the InvoiceTauBench LLM judge against a saved report.")
    parser.add_argument("--report-dir", required=True, help="Directory containing latest.json and/or runs/*.json.")
    parser.add_argument("--write", action="store_true", help="Overwrite latest.json/latest.md and run JSON files with judge scores.")
    parser.add_argument("--require-llm-judge", action="store_true", help="Mark scenario contract pass as failed when judge score fails.")
    args = parser.parse_args()

    report_dir = Path(args.report_dir).resolve()
    results = _load_results(report_dir)
    if not results:
        raise SystemExit(f"No InvoiceTauBench run results found in {report_dir}.")

    scored: list[ScenarioRunResult] = []
    for result in results:
        _scenario, expected, _scenario_dir = load_scenario(SCENARIOS_ROOT / result.scenario_id)
        case_dir = report_dir / "workspace" / "cases" / result.case_id
        checks = verify_run(result, expected, case_dir)
        passed, deterministic_score = score_checks(checks)
        result.checks = checks
        result.passed = passed
        result.score = deterministic_score
        result.llm_judge = run_llm_judge(result, expected, enabled=True)
        if args.require_llm_judge and result.llm_judge.enabled and not result.llm_judge.error:
            result.passed = bool(result.passed and result.llm_judge.passed)
            result.score = round((result.score + result.llm_judge.score) / 2, 4)
        scored.append(result)
        status = "PASS" if result.passed else "FAIL"
        judge = f"{result.llm_judge.score:.3f}" if not result.llm_judge.error else f"error: {result.llm_judge.error[:80]}"
        print(f"[{status}] {result.scenario_id} run={result.run_index} contract={result.score:.3f} judge={judge}")

    previous = _load_summary(report_dir)
    summary = summarize_results(
        scored,
        mode=previous.mode if previous else (scored[0].mode if scored else "live"),
        k=max(1, max((item.run_index for item in scored), default=1)),
        profile=previous.profile if previous else "",
    )
    if args.write:
        for result in scored:
            write_json(report_dir / "runs" / f"{result.scenario_id}_{result.run_index}.json", result.model_dump(mode="json"))
        write_json(report_dir / "latest.json", summary.model_dump(mode="json"))
        write_markdown(report_dir / "latest.md", render_markdown(summary))
    print(
        "contract_pass@1="
        f"{summary.contract_pass_at_1:.3f} deterministic_score={summary.deterministic_score:.3f} "
        f"llm_quality_score={summary.llm_quality_score if summary.llm_quality_score is not None else 'n/a'} "
        f"judge_errors={summary.judge_error_count}"
    )
    return 0 if summary.judge_error_count == 0 else 1


def _load_results(report_dir: Path) -> list[ScenarioRunResult]:
    summary = _load_summary(report_dir)
    if summary and summary.scenario_results:
        return list(summary.scenario_results)
    runs_dir = report_dir / "runs"
    if not runs_dir.exists():
        return []
    return [
        ScenarioRunResult.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted(runs_dir.glob("*.json"))
        if path.is_file()
    ]


def _load_summary(report_dir: Path) -> BenchmarkSummary | None:
    path = report_dir / "latest.json"
    if not path.exists():
        return None
    return BenchmarkSummary.model_validate_json(path.read_text(encoding="utf-8"))


def _configure_utf8_stdio() -> None:
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    raise SystemExit(main())
