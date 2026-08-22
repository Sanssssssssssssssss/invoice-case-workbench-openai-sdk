from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from math import ceil
from pathlib import Path
from typing import Any, Iterable, Mapping

from app.state.persistence import atomic_write_text

from .models import EvalResult, EvalSnapshot, load_case, validate_case_input
from .runner import (
    DEFAULT_CASES_ROOT,
    DEFAULT_EVAL_ARTIFACT_ROOT,
    BusinessEvalPaths,
    RuntimeFactory,
    run_business_eval,
    score_revision_id,
    score_snapshot,
)


DEFAULT_BENCHMARK_ROOT = DEFAULT_EVAL_ARTIFACT_ROOT / "benchmarks"
STAGE_ORDER = ("understanding", "evidence", "reasoning", "proof", "report", "communication")
STAGE_LABELS = {
    "understanding": "任务理解",
    "evidence": "证据与来源",
    "reasoning": "业务核查",
    "proof": "Proof 与投影",
    "report": "业务报告",
    "communication": "中文沟通",
}
DEV_SUITES = {"atomic_dev", "composite_dev"}
ENGINEERING_TOTAL_KEYS = (
    "provider_calls",
    "role_calls",
    "api_prompt_tokens",
    "api_completion_tokens",
    "api_total_tokens",
    "api_cached_tokens",
    "role_total_tokens",
    "role_cached_tokens",
    "duration_ms",
    "error_events",
    "blocked_actions",
    "hook_rejections",
    "tool_calls",
    "tool_error_calls",
    "report_count",
    "report_bytes",
)
MAX_REPAIR_ITEMS_IN_REPORT = 20


@dataclass(frozen=True)
class BusinessBenchmarkPaths:
    run_dir: Path
    summary: Path
    report: Path
    case_runs: tuple[BusinessEvalPaths, ...]


def discover_business_cases(
    root: Path = DEFAULT_CASES_ROOT,
    *,
    case_ids: Iterable[str] = (),
    suites: Iterable[str] = (),
) -> list[Path]:
    """Discover hash-valid runtime inputs without loading hidden oracles."""

    root = root.resolve()
    selected_ids = {item for item in case_ids if item}
    selected_suites = {item for item in suites if item}
    candidates = (
        [root / case_id for case_id in sorted(selected_ids)]
        if selected_ids
        else sorted(path for path in root.iterdir() if path.is_dir() and (path / "case.json").is_file())
        if root.is_dir()
        else []
    )
    result: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for path in candidates:
        case = validate_case_input(path)
        if case.case_id in seen:
            raise ValueError(f"duplicate business eval case_id: {case.case_id}")
        seen.add(case.case_id)
        if selected_suites and case.suite not in selected_suites:
            continue
        result.append((case.case_id, path.resolve()))
    return [path for _case_id, path in sorted(result)]


async def run_business_benchmark(
    case_dirs: Iterable[Path],
    *,
    output_dir: Path | None = None,
    runtime_factory: RuntimeFactory | None = None,
) -> BusinessBenchmarkPaths:
    """Run cases sequentially because the production runner isolates process settings."""

    resolved = [Path(path).resolve() for path in case_dirs]
    if not resolved:
        raise ValueError("business benchmark requires at least one case")
    cases = [validate_case_input(path) for path in resolved]
    _reject_duplicate_ids(case.case_id for case in cases)
    run_dir = _new_run_dir(output_dir)
    case_runs: list[BusinessEvalPaths] = []
    for case_dir in resolved:
        case_runs.append(
            await run_business_eval(
                case_dir,
                output_root=run_dir,
                runtime_factory=runtime_factory,
            )
        )
    return write_business_benchmark(
        run_dir,
        case_runs,
        case_dirs={case.case_id: path for case, path in zip(cases, resolved)},
    )


def rescore_business_benchmark(
    snapshot_root: Path,
    *,
    cases_root: Path = DEFAULT_CASES_ROOT,
    output_dir: Path | None = None,
) -> BusinessBenchmarkPaths:
    """Re-score every saved snapshot under a root without constructing a runtime."""

    snapshot_root = snapshot_root.resolve()
    snapshot_paths = sorted(snapshot_root.rglob("snapshot.json")) if snapshot_root.is_dir() else []
    if not snapshot_paths:
        raise ValueError(f"no snapshot.json found under {snapshot_root}")
    run_dir = _new_run_dir(output_dir)
    case_dirs: dict[str, Path] = {}
    case_runs: list[BusinessEvalPaths] = []
    for snapshot_path in snapshot_paths:
        snapshot = EvalSnapshot.model_validate_json(snapshot_path.read_text(encoding="utf-8"))
        case_dir = (cases_root / snapshot.case_id).resolve()
        validate_case_input(case_dir)
        case_dirs[snapshot.case_id] = case_dir
        case_runs.append(score_snapshot(snapshot_path, case_dir=case_dir))
    return write_business_benchmark(run_dir, case_runs, case_dirs=case_dirs)


def write_business_benchmark(
    run_dir: Path,
    case_runs: Iterable[BusinessEvalPaths],
    *,
    case_dirs: Mapping[str, Path],
) -> BusinessBenchmarkPaths:
    summary_path = run_dir / "benchmark.json"
    report_path = run_dir / "benchmark_report.md"
    if summary_path.exists() or report_path.exists():
        raise FileExistsError(f"benchmark artifacts already exist: {run_dir}")
    runs = tuple(case_runs)
    summary = summarize_business_results(runs, case_dirs=case_dirs, run_dir=run_dir)
    atomic_write_text(
        summary_path,
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    atomic_write_text(report_path, render_business_benchmark(summary))
    return BusinessBenchmarkPaths(run_dir, summary_path, report_path, runs)


def summarize_business_results(
    case_runs: Iterable[BusinessEvalPaths],
    *,
    case_dirs: Mapping[str, Path],
    run_dir: Path,
) -> dict[str, Any]:
    case_runs = tuple(case_runs)
    rows: list[dict[str, Any]] = []
    stage_totals = {
        stage: {"earned": Decimal("0"), "possible": Decimal("0")} for stage in STAGE_ORDER
    }
    engineering = {key: 0 for key in ENGINEERING_TOTAL_KEYS}
    veto_counts: Counter[str] = Counter()
    first_failure_counts: Counter[str] = Counter()
    repair_groups: dict[tuple[str, str, str, bool], dict[str, Any]] = {}
    scorer_versions: set[str] = set()
    oracle_versions: set[str] = set()
    outcome_counts = {
        "target_truth": 0,
        "report": 0,
        "communication": 0,
        "runtime_completed": 0,
    }
    framework_scores: list[Decimal] = []
    framework_enabled_runs = 0
    framework_passed_runs = 0
    seen_runs: set[tuple[str, str]] = set()

    for paths in case_runs:
        snapshot = EvalSnapshot.model_validate_json(paths.snapshot.read_text(encoding="utf-8"))
        result = EvalResult.model_validate_json(paths.score.read_text(encoding="utf-8"))
        if (snapshot.case_id, snapshot.run_id) != (result.case_id, result.run_id):
            raise ValueError(f"snapshot and score identity mismatch: {paths.run_dir}")
        run_identity = (snapshot.case_id, snapshot.run_id)
        if run_identity in seen_runs:
            raise ValueError(f"duplicate business eval run: {run_identity}")
        seen_runs.add(run_identity)
        case_dir = case_dirs.get(result.case_id)
        if case_dir is None:
            raise ValueError(f"missing case directory for {result.case_id}")
        case = load_case(case_dir)
        revision = _validated_revision(
            paths,
            snapshot=snapshot,
            result=result,
            case_dir=case_dir,
            benchmark_root=run_dir,
        )
        scorer_versions.add(result.scorer_version)
        oracle_versions.add(result.oracle_version)
        failed = [item for item in result.checks if not item.passed]
        failed_core = [item for item in failed if item.core]
        failed_framework = [item for item in result.framework_checks if not item.passed]
        if result.framework_enabled:
            framework_enabled_runs += 1
            framework_passed_runs += int(result.framework_passed)
            framework_scores.append(result.framework_score)
        target_truth = next(
            (item.passed for item in result.checks if item.id == "proof.target_decision_truth"),
            False,
        )
        report_checks = [
            item for item in result.checks if item.stage == "report" and item.core
        ]
        communication_checks = [
            item for item in result.checks if item.stage == "communication" and item.core
        ]
        report_ok = bool(report_checks) and all(item.passed for item in report_checks)
        communication_ok = bool(communication_checks) and all(
            item.passed for item in communication_checks
        )
        runtime_completed = not snapshot.runtime_error and not any(
            item.code == "RUN_FAILED" for item in result.vetoes
        )
        efficiency = _run_efficiency(snapshot, result)
        outcome_counts["target_truth"] += int(target_truth)
        outcome_counts["report"] += int(report_ok)
        outcome_counts["communication"] += int(communication_ok)
        outcome_counts["runtime_completed"] += int(runtime_completed)
        per_stage: dict[str, dict[str, float]] = {}
        for stage in STAGE_ORDER:
            checks = [item for item in result.checks if item.stage == stage]
            earned = sum((item.earned for item in checks), Decimal("0"))
            possible = sum((item.points for item in checks), Decimal("0"))
            stage_totals[stage]["earned"] += earned
            stage_totals[stage]["possible"] += possible
            per_stage[stage] = _score_pair(earned, possible)
        for veto in result.vetoes:
            veto_counts[veto.code] += 1
        if result.first_failed_stage:
            first_failure_counts[result.first_failed_stage] += 1
        for key in ENGINEERING_TOTAL_KEYS:
            value = result.engineering.get(key, 0)
            if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
                engineering[key] += value
        if case.suite in DEV_SUITES:
            for veto in result.vetoes:
                _add_repair_group(
                    repair_groups,
                    kind="veto",
                    item_id=veto.code,
                    stage="veto",
                    core=True,
                    case_id=case.case_id,
                    detail=veto.detail,
                )
            for check in failed:
                _add_repair_group(
                    repair_groups,
                    kind="check",
                    item_id=check.id,
                    stage=check.stage,
                    core=check.core,
                    case_id=case.case_id,
                    detail=check.detail,
                )
            for check in failed_framework:
                _add_repair_group(
                    repair_groups,
                    kind="framework",
                    item_id=check.id,
                    stage="framework",
                    core=True,
                    case_id=case.case_id,
                    detail=check.detail,
                )
        rows.append(
            {
                "case_id": case.case_id,
                "case_version": case.case_version,
                "title": case.title,
                "suite": case.suite,
                "run_id": result.run_id,
                "oracle_version": result.oracle_version,
                "passed": result.passed,
                "business_passed": result.business_passed,
                "score": float(result.score),
                "framework_enabled": result.framework_enabled,
                "framework_passed": result.framework_passed,
                "framework_score": float(result.framework_score),
                "failed_framework_check_ids": [item.id for item in failed_framework],
                "first_failed_stage": result.first_failed_stage,
                "failed_check_ids": [item.id for item in failed],
                "failed_core_check_ids": [item.id for item in failed_core],
                "veto_codes": [item.code for item in result.vetoes],
                "stages": per_stage,
                "engineering": result.engineering,
                "efficiency": efficiency,
                **revision,
                "snapshot_path": _display_path(run_dir, paths.snapshot),
                "score_path": _display_path(run_dir, paths.score),
                "eval_report_path": _display_path(run_dir, paths.report),
            }
        )

    if not rows:
        raise ValueError("business benchmark requires at least one scored run")
    rows.sort(key=lambda item: (item["case_id"], item["run_id"]))
    core_checks = [
        check
        for paths in case_runs
        for check in EvalResult.model_validate_json(paths.score.read_text(encoding="utf-8")).checks
        if check.core
    ]
    suites = _suite_breakdown(rows)
    scores = [Decimal(str(item["score"])) for item in rows]
    efficiency_summary = _summarize_efficiency(rows)
    for row in rows:
        row["efficiency"].pop("_model_latency_samples_ms", None)
        row["efficiency"].pop("_ttft_samples_ms", None)
        for values in row["efficiency"]["by_role"].values():
            values.pop("_latency_samples_ms", None)
            values.pop("_ttft_samples_ms", None)
    return {
        "schema_version": "1",
        "benchmark": "BusinessEval",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "strict_pass": all(item["passed"] for item in rows),
        "case_count": len({item["case_id"] for item in rows}),
        "run_count": len(rows),
        "passed_runs": sum(1 for item in rows if item["passed"]),
        "outcomes": {
            key: {
                "passed": count,
                "total": len(rows),
                "pass_rate": _ratio(count, len(rows)),
            }
            for key, count in outcome_counts.items()
        },
        "score_mean": _decimal_mean(scores),
        "score_min": float(min(scores)),
        "framework": {
            "enabled_runs": framework_enabled_runs,
            "passed_runs": framework_passed_runs,
            "pass_rate": _ratio(framework_passed_runs, framework_enabled_runs),
            "score_mean": _decimal_mean(framework_scores) if framework_scores else 100.0,
        },
        "core_checks": {
            "passed": sum(1 for item in core_checks if item.passed),
            "total": len(core_checks),
            "pass_rate": _ratio(sum(1 for item in core_checks if item.passed), len(core_checks)),
        },
        "stages": {
            stage: _score_pair(values["earned"], values["possible"])
            for stage, values in stage_totals.items()
        },
        "suites": suites,
        "veto_counts": dict(sorted(veto_counts.items())),
        "first_failed_stage_counts": dict(sorted(first_failure_counts.items())),
        "engineering_totals": engineering,
        **efficiency_summary,
        "scorer_versions": sorted(scorer_versions),
        "oracle_versions": sorted(oracle_versions),
        "repair_queue_source_suites": sorted(DEV_SUITES),
        "repair_queue": _sorted_repair_queue(repair_groups),
        "case_runs": rows,
    }


def render_business_benchmark(summary: Mapping[str, Any]) -> str:
    status = "PASS" if summary["strict_pass"] else "FAIL"
    core = summary["core_checks"]
    framework = summary.get("framework") or {}
    lines = [
        "# Business Benchmark 报告",
        "",
        f"- 严格结果：**{status}**（所有运行都通过才算通过）",
        f"- 案例：**{summary['case_count']}**；运行：**{summary['run_count']}**；通过：**{summary['passed_runs']}**",
        f"- 平均分：**{summary['score_mean']:.2f}**；最低分：**{summary['score_min']:.2f}**",
        f"- 核心检查：**{core['passed']}/{core['total']}**（{core['pass_rate']:.1%}）",
        (
            f"- 框架协议：**{framework.get('passed_runs', 0)}/"
            f"{framework.get('enabled_runs', 0)}**；"
            f"启用案例平均 **{framework.get('score_mean', 100):.2f}/100**（不计入业务分）"
        ),
        (
            f"- 目标三态命中：**{summary['outcomes']['target_truth']['passed']}/"
            f"{summary['outcomes']['target_truth']['total']}**；"
            f"报告完整：**{summary['outcomes']['report']['passed']}/"
            f"{summary['outcomes']['report']['total']}**；"
            f"中文交付完整：**{summary['outcomes']['communication']['passed']}/"
            f"{summary['outcomes']['communication']['total']}**；"
            f"运行完成：**{summary['outcomes']['runtime_completed']['passed']}/"
            f"{summary['outcomes']['runtime_completed']['total']}**"
        ),
        "",
        "## 套件",
        "",
        "| 套件 | 运行 | 通过 | 通过率 | 平均分 |",
        "|---|---:|---:|---:|---:|",
    ]
    for suite, values in summary["suites"].items():
        lines.append(
            f"| {_escape(suite)} | {values['runs']} | {values['passed']} | "
            f"{values['pass_rate']:.1%} | {values['score_mean']:.2f} |"
        )
    lines.extend(["", "## 阶段汇总", "", "| 阶段 | 得分 | 得分率 |", "|---|---:|---:|"])
    for stage in STAGE_ORDER:
        values = summary["stages"][stage]
        lines.append(
            f"| {STAGE_LABELS[stage]} | {values['earned']:.2f}/{values['possible']:.2f} | "
            f"{values['rate']:.1%} |"
        )
    lines.extend(
        [
            "",
            "## 案例结果",
            "",
            "| 案例 | 套件 | 结果 | 业务分 | 框架 | 首个失败阶段 | Veto | Provider Calls | Tokens | 模型 TTFT p50 ms | 耗时 ms |",
            "|---|---|---|---:|---|---|---|---:|---:|---:|---:|",
        ]
    )
    for row in summary["case_runs"]:
        metrics = row["efficiency"]
        lines.append(
            (
                "| {case} | {suite} | {status} | {score:.2f} | {framework} | {stage} | {veto} | "
                "{calls} | {tokens} | {ttft} | {duration} |"
            ).format(
                case=_escape(row["case_id"]),
                suite=_escape(row["suite"]),
                status="PASS" if row["passed"] else "FAIL",
                score=row["score"],
                framework=(
                    f"{'PASS' if row['framework_passed'] else 'FAIL'} "
                    f"{row['framework_score']:.2f}"
                    if row["framework_enabled"]
                    else "未启用"
                ),
                stage=STAGE_LABELS.get(row["first_failed_stage"], row["first_failed_stage"]) or "-",
                veto=_escape(", ".join(row["veto_codes"]) or "-"),
                calls=metrics["execution"]["provider_calls"],
                tokens=metrics["tokens"]["total_tokens"],
                ttft=_metric_text(metrics["latency"]["ttft_ms"]),
                duration=_metric_text(metrics["latency"]["e2e_duration_ms"]),
            )
        )
    lines.extend(["", "## Veto 汇总", ""])
    if summary["veto_counts"]:
        lines.extend(f"- `{code}`：{count}" for code, count in summary["veto_counts"].items())
    else:
        lines.append("- 无")
    lines.extend(["", "## 下一步修复队列（仅 Dev）", ""])
    lines.append("该队列只使用 `atomic_dev` 和 `composite_dev`；holdout 结果不生成定向调优建议。")
    lines.append("")
    if summary["repair_queue"]:
        visible_repairs = summary["repair_queue"][:MAX_REPAIR_ITEMS_IN_REPORT]
        for index, item in enumerate(visible_repairs, start=1):
            cases = ", ".join(item["case_ids"])
            lines.append(
                f"{index}. `{item['id']}`（{item['stage']}，{'核心' if item['core'] else '非核心'}，"
                f"{item['count']} 次；案例：{cases}）"
            )
        omitted = len(summary["repair_queue"]) - len(visible_repairs)
        if omitted:
            lines.append(f"- 其余 {omitted} 个低频诊断保留在 `benchmark.json`，不在短报告中展开。")
    else:
        lines.append("- Dev 案例没有失败项。")
    lines.extend(["", "## 工程指标合计", ""])
    for key, value in summary["engineering_totals"].items():
        lines.append(f"- `{key}`：{value}")
    efficiency = summary["engineering_efficiency"]
    lines.extend(
        [
            "",
            "## 效率基线",
            "",
            "效率指标不与业务分合成总分；不可观测字段保持 `null`，并在 `benchmark.json` 中记录 coverage。",
            "角色 latency 可能包含下游工具或子角色等待时间，只用于同层比较，不能跨层相加。",
            f"- 业务通过次数：`{efficiency['business_pass_count']}`",
            f"- Tokens / business pass：`{_metric_text(efficiency['tokens_per_business_pass'])}`",
            f"- Latency ms / business pass：`{_metric_text(efficiency['latency_ms_per_business_pass'])}`",
            (
                "- E2E latency p50/p95 ms："
                f"`{_metric_text(summary['engineering_distribution']['e2e_duration_ms']['p50'])}` / "
                f"`{_metric_text(summary['engineering_distribution']['e2e_duration_ms']['p95'])}`"
            ),
            "",
            "### 分层模型指标",
            "",
            "| 层/角色 | Provider Calls | Logical Calls | Input | Output | Reasoning | Cached | "
            "Total | Latency p50/p95 ms | TTFT p50/p95 ms |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for role, values in summary["engineering_by_role"].items():
        lines.append(
            f"| {_escape(role)} | {_metric_text(values['provider_calls'])} | "
            f"{values['model_role_calls']} | "
            f"{_metric_text(values['input_tokens'])} | {_metric_text(values['output_tokens'])} | "
            f"{_metric_text(values['reasoning_tokens'])} | {_metric_text(values['cached_tokens'])} | "
            f"{_metric_text(values['total_tokens'])} | "
            f"{_metric_text(values['latency_p50_ms'])}/{_metric_text(values['latency_p95_ms'])} | "
            f"{_metric_text(values['ttft_p50_ms'])}/{_metric_text(values['ttft_p95_ms'])} |"
        )
    lines.extend(["", "## 单案例记录", ""])
    for row in summary["case_runs"]:
        lines.append(
            f"- `{row['case_id']}`：revision=`{row['revision_id']}`；"
            f"manifest=`{row['revision_manifest_path']}`；score=`{row['score_path']}`；"
            f"报告=`{row['eval_report_path']}`；snapshot=`{row['snapshot_path']}`"
        )
    return "\n".join(lines).rstrip() + "\n"


def _run_efficiency(snapshot: EvalSnapshot, result: EvalResult) -> dict[str, Any]:
    engineering = result.engineering
    provider_rows = [
        _map(item.get("payload"))
        for item in snapshot.events
        if isinstance(item, dict) and item.get("kind") == "provider_call"
    ]
    observability = _map(snapshot.trace.get("observability"))
    model_metrics = _map(observability.get("model_metrics"))
    raw_model_rows = model_metrics.get("calls")
    model_rows = (
        [item for item in raw_model_rows if isinstance(item, Mapping)]
        if isinstance(raw_model_rows, list)
        else []
    )

    def token(usage_key: str) -> int | None:
        if not provider_rows:
            return None
        values = [_num(_map(item.get("usage")).get(usage_key)) for item in provider_rows]
        return int(sum(values)) if all(item is not None for item in values) else None

    tokens = {
        "input_tokens": token("prompt_tokens"),
        "output_tokens": token("completion_tokens"),
        "reasoning_tokens": token("reasoning_tokens"),
        "cached_tokens": token("cached_tokens"),
        "total_tokens": token("total_tokens"),
    }
    if tokens["total_tokens"] is None and None not in (tokens["input_tokens"], tokens["output_tokens"]):
        tokens["total_tokens"] = tokens["input_tokens"] + tokens["output_tokens"]
    role_metrics = _role_metrics(provider_rows, model_rows)
    latencies = [_num(item.get("latency_ms")) for item in model_rows]
    latencies = [item for item in latencies if item is not None]
    ttfts = [_num(item.get("ttft_ms")) for item in model_rows]
    ttfts = [item for item in ttfts if item is not None]
    prompts = [_num(_map(item.get("usage")).get("prompt_tokens")) for item in provider_rows]
    prompts = [item for item in prompts if item is not None]
    compiler_run = _map(observability.get("compiler_run"))
    tool_calls = _int_or_none(engineering.get("tool_calls"))
    tool_errors = _int_or_none(engineering.get("tool_error_calls"))
    max_turn_source = list(snapshot.events)
    max_turns_observed = bool(max_turn_source)
    if not any("maxturnsexceeded" in str(item.get("summary") or "").casefold() for item in max_turn_source):
        max_turn_source = [
            item
            for item in (snapshot.trace.get("observations") or [])
            if isinstance(item, dict)
        ]
        max_turns_observed = max_turns_observed or bool(max_turn_source)
    max_turn_keys = {
        (item.get("kind"), item.get("name"), item.get("step_count"), item.get("summary"))
        for item in max_turn_source
        if isinstance(item, dict) and "maxturnsexceeded" in str(item.get("summary") or "").casefold()
    }
    input_tokens = tokens["input_tokens"]
    cached_tokens = tokens["cached_tokens"]
    trace_duration = _num(snapshot.trace.get("duration_ms"))
    return {
        "provider": snapshot.provider or None,
        "model": snapshot.model or None,
        "tokens": tokens,
        "execution": {
            "provider_calls": len(provider_rows) if provider_rows else None,
            "model_role_calls": (
                len(model_rows)
                if isinstance(model_metrics.get("calls"), list)
                else None
            ),
            "agent_turns": (
                int(snapshot.trace["step_count"])
                if snapshot.trace.get("step_count") is not None
                else None
            ),
            "tool_calls": tool_calls,
            "successful_tool_calls": (
                tool_calls - tool_errors
                if tool_calls is not None and tool_errors is not None
                else None
            ),
            "tool_error_count": tool_errors,
            "compiler_retry_count": _int_or_none(compiler_run.get("retry_count")),
            "max_turns_hits": len(max_turn_keys) if max_turns_observed else None,
        },
        "latency": {
            "e2e_duration_ms": (
                trace_duration
                if trace_duration is not None
                else _num(engineering.get("duration_ms"))
            ),
            "model_call_p50_ms": _pctl(latencies, 0.50),
            "model_call_p95_ms": _pctl(latencies, 0.95),
            "ttft_ms": _pctl(ttfts, 0.50),
            "ttft_p95_ms": _pctl(ttfts, 0.95),
            "coverage": {
                "model_latency": _coverage(len(latencies), len(model_rows)),
                "ttft": _coverage(len(ttfts), len(model_rows)),
            },
        },
        "context": {
            "peak_context_tokens": (
                max(prompts)
                if provider_rows and len(prompts) == len(provider_rows)
                else None
            ),
            "cache_hit_ratio": (
                round(cached_tokens / input_tokens, 4)
                if input_tokens and cached_tokens is not None
                else None
            ),
        },
        "by_role": role_metrics,
        "_model_latency_samples_ms": latencies,
        "_ttft_samples_ms": ttfts,
    }


def _role_metrics(provider_rows: list[Mapping[str, Any]], model_rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, dict[str, Any]] = {}
    token_pairs = (
        ("prompt_tokens", "input_tokens"),
        ("completion_tokens", "output_tokens"),
        ("reasoning_tokens", "reasoning_tokens"),
        ("cached_tokens", "cached_tokens"),
        ("total_tokens", "total_tokens"),
    )

    def new_item() -> dict[str, Any]:
        return {
            "provider_calls": 0,
            "model_role_calls": 0,
            **{target: 0 for _, target in token_pairs},
            "_latency": [],
            "_ttft": [],
            "_coverage": Counter(),
        }

    for row in provider_rows:
        role = (
            "case_manager"
            if row.get("role") in {"planner", "supervisor"}
            else str(row.get("role") or "unknown")
        )
        item = grouped.setdefault(role, new_item())
        item["provider_calls"] += 1
        usage = _map(row.get("usage"))
        for source, target in token_pairs:
            value = _num(usage.get(source))
            if value is not None:
                item[target] += value
                item["_coverage"][target] += 1
    for row in model_rows:
        role = (
            "case_manager"
            if row.get("role") in {"planner", "supervisor"}
            else str(row.get("role") or "unknown")
        )
        item = grouped.setdefault(role, new_item())
        item["model_role_calls"] += 1
        for source, target in (("latency_ms", "_latency"), ("ttft_ms", "_ttft")):
            value = _num(row.get(source))
            if value is not None:
                item[target].append(value)
    result = {}
    for role, item in sorted(grouped.items()):
        # A logical/model row does not prove that provider telemetry for the
        # same role was observed.  Preserve that gap as unknown, even when
        # other roles in this run do have provider events.
        provider_calls = item["provider_calls"] or None
        tokens = {
            key: (
                item[key]
                if provider_calls is not None
                and provider_calls > 0
                and item["_coverage"][key] == provider_calls
                else None
            )
            for _, key in token_pairs
        }
        result[role] = {
            "provider_calls": provider_calls,
            "model_role_calls": item["model_role_calls"],
            **tokens,
            "latency_p50_ms": _pctl(item["_latency"], 0.50),
            "latency_p95_ms": _pctl(item["_latency"], 0.95),
            "ttft_p50_ms": _pctl(item["_ttft"], 0.50),
            "ttft_p95_ms": _pctl(item["_ttft"], 0.95),
            "coverage": {
                key: _coverage(item["_coverage"][key], provider_calls)
                for _, key in token_pairs
            }
            | {
                "latency": _coverage(len(item["_latency"]), item["model_role_calls"]),
                "ttft": _coverage(len(item["_ttft"]), item["model_role_calls"]),
            },
            "_latency_samples_ms": item["_latency"],
            "_ttft_samples_ms": item["_ttft"],
        }
    return result


def _summarize_efficiency(rows: list[dict[str, Any]]) -> dict[str, Any]:
    totals = [row["efficiency"]["tokens"]["total_tokens"] for row in rows]
    durations = [row["efficiency"]["latency"]["e2e_duration_ms"] for row in rows]
    call_latencies = [value for row in rows for value in row["efficiency"]["_model_latency_samples_ms"]]
    ttfts = [value for row in rows for value in row["efficiency"]["_ttft_samples_ms"]]
    pass_count = sum(row["business_passed"] is True for row in rows)
    observed_tokens = [item for item in totals if item is not None]
    observed_durations = [item for item in durations if item is not None]
    return {
        "engineering_distribution": {
            "total_tokens": _dist(observed_tokens, len(rows), "runs"),
            "e2e_duration_ms": _dist(observed_durations, len(rows), "runs"),
            "model_call_latency_ms": _dist(
                call_latencies,
                sum(row["efficiency"]["execution"]["model_role_calls"] or 0 for row in rows),
                "calls",
            ),
            "ttft_ms": _dist(
                ttfts,
                sum(row["efficiency"]["execution"]["model_role_calls"] or 0 for row in rows),
                "calls",
            ),
        },
        "engineering_by_role": _aggregate_roles(rows),
        "engineering_efficiency": {
            "business_pass_count": pass_count,
            "tokens_per_business_pass": (
                round(sum(observed_tokens) / pass_count, 4)
                if pass_count and len(observed_tokens) == len(rows)
                else None
            ),
            "latency_ms_per_business_pass": (
                round(sum(observed_durations) / pass_count, 4)
                if pass_count and len(observed_durations) == len(rows)
                else None
            ),
        },
    }


def _aggregate_roles(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, dict[str, Any]] = {}
    token_keys = (
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cached_tokens",
        "total_tokens",
    )
    for row in rows:
        for role, values in row["efficiency"]["by_role"].items():
            item = grouped.setdefault(
                role,
                {
                    "provider_calls": [],
                    "model_role_calls": 0,
                    **{key: [] for key in token_keys},
                },
            )
            item.setdefault("_latency", []).extend(values["_latency_samples_ms"])
            item.setdefault("_ttft", []).extend(values["_ttft_samples_ms"])
            item["provider_calls"].append(values["provider_calls"])
            item["model_role_calls"] += values["model_role_calls"]
            for key in token_keys:
                item[key].append(values[key])
    return {
        role: {
            "provider_calls": (
                sum(values["provider_calls"])
                if values["provider_calls"]
                and all(item is not None for item in values["provider_calls"])
                else None
            ),
            "model_role_calls": values["model_role_calls"],
            **{
                key: sum(values[key]) if all(item is not None for item in values[key]) else None
                for key in token_keys
            },
            "latency_p50_ms": _pctl(values["_latency"], 0.50),
            "latency_p95_ms": _pctl(values["_latency"], 0.95),
            "ttft_p50_ms": _pctl(values["_ttft"], 0.50),
            "ttft_p95_ms": _pctl(values["_ttft"], 0.95),
            "coverage": {
                "latency": _coverage(len(values["_latency"]), values["model_role_calls"]),
                "ttft": _coverage(len(values["_ttft"]), values["model_role_calls"]),
            },
        }
        for role, values in sorted(grouped.items())
    }


def _dist(values: list[float | int], total: int, unit: str) -> dict[str, Any]:
    return {
        "p50": _pctl(values, 0.50),
        "p95": _pctl(values, 0.95),
        f"observed_{unit}": len(values),
        f"total_{unit}": total,
    }


def _coverage(observed: int, total: int | None) -> dict[str, int | None]:
    return {"observed": observed, "total": total}


def _pctl(values: list[float | int], quantile: float) -> float | int | None:
    ordered = sorted(values)
    return ordered[max(0, ceil(quantile * len(ordered)) - 1)] if ordered else None


def _num(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return float(value)
    return value if isinstance(value, (int, float)) else None


def _int_or_none(value: Any) -> int | None:
    value = _num(value)
    return int(value) if value is not None else None


def _map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _metric_text(value: Any) -> str:
    return "-" if value is None else str(value)


def _suite_breakdown(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["suite"]].append(row)
    return {
        suite: {
            "runs": len(items),
            "passed": sum(1 for item in items if item["passed"]),
            "pass_rate": _ratio(sum(1 for item in items if item["passed"]), len(items)),
            "score_mean": _decimal_mean([Decimal(str(item["score"])) for item in items]),
        }
        for suite, items in sorted(grouped.items())
    }


def _add_repair_group(
    groups: dict[tuple[str, str, str, bool], dict[str, Any]],
    *,
    kind: str,
    item_id: str,
    stage: str,
    core: bool,
    case_id: str,
    detail: str,
) -> None:
    key = (kind, item_id, stage, core)
    group = groups.setdefault(
        key,
        {"kind": kind, "id": item_id, "stage": stage, "core": core, "case_ids": set(), "detail": detail},
    )
    group["case_ids"].add(case_id)


def _sorted_repair_queue(groups: Mapping[tuple[str, str, str, bool], dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for item in groups.values():
        case_ids = sorted(item["case_ids"])
        result.append({**item, "count": len(case_ids), "case_ids": case_ids})
    order = {stage: index for index, stage in enumerate(STAGE_ORDER)}
    return sorted(
        result,
        key=lambda item: (
            -1 if item["kind"] == "veto" else order.get(item["stage"], len(order)),
            not item["core"],
            -item["count"],
            item["id"],
        ),
    )


def _score_pair(earned: Decimal, possible: Decimal) -> dict[str, float]:
    return {
        "earned": float(earned),
        "possible": float(possible),
        "rate": float(earned / possible) if possible else 0.0,
    }


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _decimal_mean(values: list[Decimal]) -> float:
    return float(sum(values, Decimal("0")) / len(values)) if values else 0.0


def _display_path(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _validated_revision(
    paths: BusinessEvalPaths,
    *,
    snapshot: EvalSnapshot,
    result: EvalResult,
    case_dir: Path,
    benchmark_root: Path,
) -> dict[str, str]:
    """Validate and pin the exact immutable scoring revision used by a row."""

    revision_path = getattr(paths, "revision", None)
    if not isinstance(revision_path, Path) or not revision_path.is_file():
        raise ValueError(f"scored run is missing revision.json: {paths.run_dir}")
    try:
        raw = json.loads(revision_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid scoring revision manifest: {revision_path}") from exc
    manifest = _required_mapping(raw, "revision")
    revision_id = _required_text(manifest, "revision_id", "revision")
    if not re.fullmatch(r"score_[0-9a-f]{20}", revision_id):
        raise ValueError("revision_id must be a content-addressed score revision")
    if score_revision_id(manifest) != revision_id:
        raise ValueError("revision_id does not match scoring inputs")
    if revision_path.parent.name != revision_id:
        raise ValueError("revision_id does not match revision directory")

    execution = _required_mapping(manifest.get("execution"), "revision.execution")
    if _required_text(execution, "run_id", "revision.execution") != snapshot.run_id:
        raise ValueError("revision run_id does not match snapshot")
    snapshot_sha256 = _sha256_file(paths.snapshot)
    if _required_text(execution, "snapshot_sha256", "revision.execution") != snapshot_sha256:
        raise ValueError("revision snapshot_sha256 mismatch")
    run_manifest_sha256 = str(execution.get("run_manifest_sha256") or "")
    if run_manifest_sha256:
        run_manifest_path = paths.snapshot.parent / "run_manifest.json"
        if run_manifest_sha256 != _sha256_file(run_manifest_path):
            raise ValueError("revision run_manifest_sha256 mismatch")

    inputs = _required_mapping(manifest.get("inputs"), "revision.inputs")
    for name, path in (("case_sha256", case_dir / "case.json"), ("oracle_sha256", case_dir / "oracle.json")):
        if _required_text(inputs, name, "revision.inputs") != _sha256_file(path):
            raise ValueError(f"revision {name} mismatch")

    scoring = _required_mapping(manifest.get("scoring"), "revision.scoring")
    if _required_text(scoring, "scorer_version", "revision.scoring") != result.scorer_version:
        raise ValueError("revision scorer version does not match score")
    if str(scoring.get("oracle_version") or "") != result.oracle_version:
        raise ValueError("revision oracle version does not match score")
    code = _required_mapping(scoring.get("code"), "revision.scoring.code")
    if not re.fullmatch(
        r"[0-9a-f]{64}",
        _required_text(code, "fingerprint", "revision.scoring.code"),
    ):
        raise ValueError("revision scoring code fingerprint must be sha256")

    artifacts = _required_mapping(manifest.get("artifacts"), "revision.artifacts")
    artifact_hashes: dict[str, str] = {}
    for name, expected_path in (("score", paths.score), ("report", paths.report)):
        artifact = _required_mapping(artifacts.get(name), f"revision.artifacts.{name}")
        recorded_path = _required_text(artifact, "path", f"revision.artifacts.{name}")
        resolved_path = _resolve_revision_artifact(revision_path, recorded_path)
        if not _is_within(resolved_path, revision_path.parent):
            raise ValueError(f"revision {name} path must stay within revision directory")
        if resolved_path != expected_path.resolve():
            raise ValueError(f"revision {name} path does not match scored run")
        digest = _sha256_file(expected_path)
        if _required_text(artifact, "sha256", f"revision.artifacts.{name}") != digest:
            raise ValueError(f"revision {name} sha256 mismatch")
        artifact_hashes[name] = digest

    if (snapshot.case_id, snapshot.case_version, snapshot.run_id) != (
        result.case_id,
        result.case_version,
        result.run_id,
    ):
        raise ValueError("revision snapshot and score identity mismatch")
    return {
        "revision_id": revision_id,
        "revision_manifest_path": _display_path(benchmark_root, revision_path),
        "revision_manifest_sha256": _sha256_file(revision_path),
        "run_manifest_sha256": run_manifest_sha256,
        "snapshot_sha256": snapshot_sha256,
        "score_sha256": artifact_hashes["score"],
        "report_sha256": artifact_hashes["report"],
    }


def _required_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _required_text(value: Mapping[str, Any], key: str, label: str) -> str:
    text = str(value.get(key) or "").strip()
    if not text:
        raise ValueError(f"{label}.{key} must not be empty")
    return text


def _sha256_file(path: Path) -> str:
    if not path.is_file():
        raise ValueError(f"revision artifact does not exist: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resolve_revision_artifact(revision_path: Path, recorded_path: str) -> Path:
    candidate = Path(recorded_path)
    if not candidate.is_absolute():
        candidate = revision_path.parent / candidate
    return candidate.resolve()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _reject_duplicate_ids(case_ids: Iterable[str]) -> None:
    counts = Counter(case_ids)
    duplicates = sorted(case_id for case_id, count in counts.items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate business eval case_id: {duplicates}")


def _new_run_dir(output_dir: Path | None) -> Path:
    run_dir = (
        output_dir.resolve()
        if output_dir
        else DEFAULT_BENCHMARK_ROOT
        / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    )
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def _escape(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
