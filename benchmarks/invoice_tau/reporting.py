from __future__ import annotations

from collections import defaultdict
from statistics import mean
from typing import Iterable

from .models import BenchmarkSummary, ScenarioRunResult


def summarize_results(results: list[ScenarioRunResult], *, mode: str, k: int, profile: str = "") -> BenchmarkSummary:
    passed = [item for item in results if item.passed]
    deterministic_score = round(mean([item.score for item in results]) if results else 0.0, 4)
    judged = [item for item in results if item.llm_judge.enabled and not item.llm_judge.error]
    judge_dimension_scores = _judge_dimension_means(judged)
    llm_quality_score = round(mean([item.llm_judge.score for item in judged]), 4) if judged else None
    by_scenario: dict[str, list[ScenarioRunResult]] = defaultdict(list)
    for item in results:
        by_scenario[item.scenario_id].append(item)
    pass_all_k = 0
    for rows in by_scenario.values():
        if rows and all(row.passed for row in rows):
            pass_all_k += 1
    scenario_count = len(by_scenario)
    return BenchmarkSummary(
        profile=profile,
        mode=mode,
        scenario_count=scenario_count,
        total_runs=len(results),
        passed_runs=len(passed),
        pass_at_1=round(len(passed) / max(1, len(results)), 4),
        contract_pass_at_1=round(len(passed) / max(1, len(results)), 4),
        pass_all_k=round(pass_all_k / max(1, scenario_count), 4) if k > 1 else round(len(passed) / max(1, len(results)), 4),
        average_score=deterministic_score,
        deterministic_score=deterministic_score,
        judged_runs=len(judged),
        llm_quality_score=llm_quality_score,
        judge_dimension_scores=judge_dimension_scores,
        judge_error_count=sum(1 for item in results if item.llm_judge.enabled and item.llm_judge.error),
        total_tokens=sum(int(item.metrics.get("total_tokens") or 0) for item in results),
        total_wall_time_ms=round(sum(float(item.metrics.get("wall_time_ms") or 0.0) for item in results), 2),
        scenario_results=results,
    )


def render_markdown(summary: BenchmarkSummary) -> str:
    lines: list[str] = []
    lines.append(f"# {summary.benchmark} Report")
    lines.append("")
    lines.append(f"- mode: `{summary.mode}`")
    if summary.profile:
        lines.append(f"- profile: `{summary.profile}`")
    lines.append(f"- scenarios: {summary.scenario_count}")
    lines.append(f"- total runs: {summary.total_runs}")
    lines.append(f"- contract_pass@1: {summary.contract_pass_at_1:.3f}")
    lines.append(f"- pass_all_k: {summary.pass_all_k:.3f}")
    lines.append(f"- deterministic_score: {summary.deterministic_score:.3f}")
    lines.append(f"- llm_quality_score: {_score_or_dash(summary.llm_quality_score)}")
    lines.append(f"- judged runs: {summary.judged_runs}")
    lines.append(f"- judge errors: {summary.judge_error_count}")
    lines.append(f"- total tokens: {summary.total_tokens}")
    lines.append(f"- total wall time ms: {summary.total_wall_time_ms:.0f}")
    if summary.judge_dimension_scores:
        lines.append("")
        lines.append("## LLM Judge Dimension Means")
        lines.append("")
        for name, value in summary.judge_dimension_scores.items():
            lines.append(f"- `{name}`: {value:.3f}")
    lines.append("")
    lines.append("## Scenario Results")
    lines.append("")
    lines.append("| scenario | category | run | contract pass | deterministic | judge score | judge pass | status | missing/conflict | calls | wall ms | tokens | failed checks | judge notes |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---|---|---|---:|---:|---|---|")
    for item in summary.scenario_results:
        failed = [check.name for check in item.checks if not check.passed]
        judge_score = "-"
        judge_pass = "-"
        judge_notes = "-"
        if item.llm_judge.enabled:
            judge_score = f"{item.llm_judge.score:.3f}" if not item.llm_judge.error else "error"
            judge_pass = "yes" if item.llm_judge.passed and not item.llm_judge.error else "no"
            judge_notes = item.llm_judge.error or item.llm_judge.rationale or ", ".join(item.llm_judge.review_flags[:3])
        metrics = item.metrics or {}
        missing_conflict = ", ".join((metrics.get("missing_requirements") or []) + (metrics.get("conflict_requirements") or []))
        calls = f"m{metrics.get('model_calls', 0)}/t{metrics.get('tool_calls', 0)}/r{metrics.get('role_calls', 0)}"
        lines.append(
            "| "
            + " | ".join(
                [
                    item.scenario_id,
                    item.category or "-",
                    str(item.run_index),
                    "yes" if item.passed else "no",
                    f"{item.score:.3f}",
                    judge_score,
                    judge_pass,
                    str(metrics.get("final_status") or ""),
                    missing_conflict or "-",
                    calls,
                    str(int(float(item.metrics.get("wall_time_ms") or 0))),
                    str(int(item.metrics.get("total_tokens") or 0)),
                    ", ".join(failed[:5]) + (" ..." if len(failed) > 5 else ""),
                    _compact(judge_notes, max_chars=140),
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
    low_quality = [
        item
        for item in summary.scenario_results
        if item.llm_judge.enabled and not item.llm_judge.error and item.llm_judge.score < item.llm_judge.pass_threshold
    ]
    if low_quality:
        lines.append("## Low LLM Quality Scores")
        lines.append("")
        for item in low_quality:
            lines.append(
                f"- `{item.scenario_id}` run {item.run_index}: "
                f"score={item.llm_judge.score:.3f}, threshold={item.llm_judge.pass_threshold:.3f}, "
                f"rationale={_compact(item.llm_judge.rationale)}"
            )
        lines.append("")
    return "\n".join(lines)


def _compact(value: object, max_chars: int = 260) -> str:
    text = str(value)
    return text if len(text) <= max_chars else text[: max_chars - 3] + "..."


def _judge_dimension_means(results: list[ScenarioRunResult]) -> dict[str, float]:
    values: dict[str, list[float]] = {}
    for item in results:
        for key, value in item.llm_judge.dimensions.items():
            values.setdefault(key, []).append(float(value))
    return {key: round(mean(rows), 4) for key, rows in sorted(values.items()) if rows}


def _score_or_dash(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"
