from __future__ import annotations

from collections import defaultdict
from statistics import mean
from typing import Iterable

from .models import BenchmarkSummary, ScenarioRunResult


def summarize_results(results: list[ScenarioRunResult], *, mode: str, k: int) -> BenchmarkSummary:
    passed = [item for item in results if item.passed]
    by_scenario: dict[str, list[ScenarioRunResult]] = defaultdict(list)
    for item in results:
        by_scenario[item.scenario_id].append(item)
    pass_all_k = 0
    for rows in by_scenario.values():
        if rows and all(row.passed for row in rows):
            pass_all_k += 1
    scenario_count = len(by_scenario)
    return BenchmarkSummary(
        mode=mode,
        scenario_count=scenario_count,
        total_runs=len(results),
        passed_runs=len(passed),
        pass_at_1=round(len(passed) / max(1, len(results)), 4),
        pass_all_k=round(pass_all_k / max(1, scenario_count), 4) if k > 1 else round(len(passed) / max(1, len(results)), 4),
        average_score=round(mean([item.score for item in results]) if results else 0.0, 4),
        total_tokens=sum(int(item.metrics.get("total_tokens") or 0) for item in results),
        total_wall_time_ms=round(sum(float(item.metrics.get("wall_time_ms") or 0.0) for item in results), 2),
        scenario_results=results,
    )


def render_markdown(summary: BenchmarkSummary) -> str:
    lines: list[str] = []
    lines.append(f"# {summary.benchmark} Report")
    lines.append("")
    lines.append(f"- mode: `{summary.mode}`")
    lines.append(f"- scenarios: {summary.scenario_count}")
    lines.append(f"- total runs: {summary.total_runs}")
    lines.append(f"- pass@1: {summary.pass_at_1:.3f}")
    lines.append(f"- pass_all_k: {summary.pass_all_k:.3f}")
    lines.append(f"- average score: {summary.average_score:.3f}")
    lines.append(f"- total tokens: {summary.total_tokens}")
    lines.append(f"- total wall time ms: {summary.total_wall_time_ms:.0f}")
    lines.append("")
    lines.append("## Scenario Results")
    lines.append("")
    lines.append("| scenario | run | pass | score | wall ms | tokens | failed checks | judge |")
    lines.append("|---|---:|---:|---:|---:|---:|---|---|")
    for item in summary.scenario_results:
        failed = [check.name for check in item.checks if not check.passed]
        judge = "-"
        if item.llm_judge.enabled:
            judge = f"{item.llm_judge.score:.2f}" if not item.llm_judge.error else f"error: {item.llm_judge.error[:60]}"
        lines.append(
            "| "
            + " | ".join(
                [
                    item.scenario_id,
                    str(item.run_index),
                    "yes" if item.passed else "no",
                    f"{item.score:.3f}",
                    str(int(float(item.metrics.get("wall_time_ms") or 0))),
                    str(int(item.metrics.get("total_tokens") or 0)),
                    ", ".join(failed[:5]) + (" ..." if len(failed) > 5 else ""),
                    judge,
                ]
            )
            + " |"
        )
    lines.append("")
    lines.append("## Failed Checks")
    lines.append("")
    for item in summary.scenario_results:
        failed = [check for check in item.checks if not check.passed]
        if not failed:
            continue
        lines.append(f"### {item.scenario_id} run {item.run_index}")
        for check in failed:
            lines.append(f"- `{check.name}` score={check.score:.3f} details=`{_compact(check.details)}`")
        lines.append("")
    return "\n".join(lines)


def _compact(value: object, max_chars: int = 260) -> str:
    text = str(value)
    return text if len(text) <= max_chars else text[: max_chars - 3] + "..."
