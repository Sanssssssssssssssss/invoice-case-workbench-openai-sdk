from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import EvalResult, EvalSnapshot, load_case, validate_case_input
from .runner import (
    DEFAULT_CASES_ROOT,
    REPO_ROOT,
    BusinessEvalPaths,
    RuntimeFactory,
    run_business_eval,
    score_snapshot,
)


DEFAULT_BENCHMARK_ROOT = REPO_ROOT / "output" / "business_benchmarks"
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
    case_dirs: dict[str, Path] = {}
    case_runs: list[BusinessEvalPaths] = []
    for snapshot_path in snapshot_paths:
        snapshot = EvalSnapshot.model_validate_json(snapshot_path.read_text(encoding="utf-8"))
        case_dir = (cases_root / snapshot.case_id).resolve()
        validate_case_input(case_dir)
        case_dirs[snapshot.case_id] = case_dir
        case_runs.append(score_snapshot(snapshot_path, case_dir=case_dir))
    run_dir = output_dir.resolve() if output_dir else snapshot_root
    run_dir.mkdir(parents=True, exist_ok=True)
    return write_business_benchmark(run_dir, case_runs, case_dirs=case_dirs)


def write_business_benchmark(
    run_dir: Path,
    case_runs: Iterable[BusinessEvalPaths],
    *,
    case_dirs: Mapping[str, Path],
) -> BusinessBenchmarkPaths:
    runs = tuple(case_runs)
    summary = summarize_business_results(runs, case_dirs=case_dirs, run_dir=run_dir)
    summary_path = run_dir / "benchmark.json"
    report_path = run_dir / "benchmark_report.md"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    report_path.write_text(render_business_benchmark(summary), encoding="utf-8")
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
    outcome_counts = {
        "target_truth": 0,
        "report": 0,
        "communication": 0,
        "runtime_completed": 0,
    }

    for paths in case_runs:
        snapshot = EvalSnapshot.model_validate_json(paths.snapshot.read_text(encoding="utf-8"))
        result = EvalResult.model_validate_json(paths.score.read_text(encoding="utf-8"))
        if (snapshot.case_id, snapshot.run_id) != (result.case_id, result.run_id):
            raise ValueError(f"snapshot and score identity mismatch: {paths.run_dir}")
        case_dir = case_dirs.get(result.case_id)
        if case_dir is None:
            raise ValueError(f"missing case directory for {result.case_id}")
        case = load_case(case_dir)
        scorer_versions.add(result.scorer_version)
        failed = [item for item in result.checks if not item.passed]
        failed_core = [item for item in failed if item.core]
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
        rows.append(
            {
                "case_id": case.case_id,
                "case_version": case.case_version,
                "title": case.title,
                "suite": case.suite,
                "run_id": result.run_id,
                "passed": result.passed,
                "score": float(result.score),
                "first_failed_stage": result.first_failed_stage,
                "failed_check_ids": [item.id for item in failed],
                "failed_core_check_ids": [item.id for item in failed_core],
                "veto_codes": [item.code for item in result.vetoes],
                "stages": per_stage,
                "engineering": result.engineering,
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
        "scorer_versions": sorted(scorer_versions),
        "repair_queue_source_suites": sorted(DEV_SUITES),
        "repair_queue": _sorted_repair_queue(repair_groups),
        "case_runs": rows,
    }


def render_business_benchmark(summary: Mapping[str, Any]) -> str:
    status = "PASS" if summary["strict_pass"] else "FAIL"
    core = summary["core_checks"]
    lines = [
        "# Business Benchmark 报告",
        "",
        f"- 严格结果：**{status}**（所有运行都通过才算通过）",
        f"- 案例：**{summary['case_count']}**；运行：**{summary['run_count']}**；通过：**{summary['passed_runs']}**",
        f"- 平均分：**{summary['score_mean']:.2f}**；最低分：**{summary['score_min']:.2f}**",
        f"- 核心检查：**{core['passed']}/{core['total']}**（{core['pass_rate']:.1%}）",
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
            "| 案例 | 套件 | 结果 | 得分 | 首个失败阶段 | Veto | Calls | Tokens | 耗时 ms |",
            "|---|---|---|---:|---|---|---:|---:|---:|",
        ]
    )
    for row in summary["case_runs"]:
        metrics = row["engineering"]
        lines.append(
            (
                "| {case} | {suite} | {status} | {score:.2f} | {stage} | {veto} | "
                "{calls} | {tokens} | {duration} |"
            ).format(
                case=_escape(row["case_id"]),
                suite=_escape(row["suite"]),
                status="PASS" if row["passed"] else "FAIL",
                score=row["score"],
                stage=STAGE_LABELS.get(row["first_failed_stage"], row["first_failed_stage"]) or "-",
                veto=_escape(", ".join(row["veto_codes"]) or "-"),
                calls=metrics.get("provider_calls", 0),
                tokens=metrics.get("api_total_tokens", 0),
                duration=metrics.get("duration_ms", 0),
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
    lines.extend(["", "## 单案例记录", ""])
    for row in summary["case_runs"]:
        lines.append(
            f"- `{row['case_id']}`：score=`{row['score_path']}`；报告=`{row['eval_report_path']}`；"
            f"snapshot=`{row['snapshot_path']}`"
        )
    return "\n".join(lines).rstrip() + "\n"


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
