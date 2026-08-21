from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from app.evals.business import benchmark
from app.evals.business.models import EvalResult, EvalSnapshot, EvalVeto, ScoreCheck
from app.evals.business.runner import BusinessEvalPaths


def _write_case(root: Path, case_id: str, suite: str) -> Path:
    case_dir = root / case_id
    attachment_dir = case_dir / "attachments"
    attachment_dir.mkdir(parents=True)
    attachment = attachment_dir / "invoice.pdf"
    attachment.write_bytes(b"%PDF-1.4\nbenchmark\n%%EOF")
    digest = hashlib.sha256(attachment.read_bytes()).hexdigest()
    (case_dir / "case.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "case_id": case_id,
                "case_version": "1",
                "title": f"案例 {case_id}",
                "suite": suite,
                "business_holes": ["invoice_arithmetic"],
                "user_message": "请审核发票并生成中文报告。",
                "policy_version": "policy_test_v1",
                "report_required": True,
                "auto_approve_tools": ["write_case_file", "render_pdf"],
                "attachments": [
                    {
                        "id": "invoice",
                        "path": "attachments/invoice.pdf",
                        "sha256": digest,
                        "content_type": "application/pdf",
                        "source_url": "https://example.invalid/invoice.pdf",
                        "source_revision": "test",
                        "license": "MIT",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return case_dir


def _write_scored_run(
    output_root: Path,
    case_id: str,
    *,
    passed: bool,
    score: int,
    failed_check: str,
    veto: str,
    provider_calls: int,
    snapshot_trace: dict[str, Any] | None = None,
    snapshot_events: list[dict[str, Any]] | None = None,
    engineering: dict[str, Any] | None = None,
    business_passed: bool | None = None,
) -> BusinessEvalPaths:
    run_dir = output_root / case_id / "run_1"
    run_dir.mkdir(parents=True, exist_ok=True)
    snapshot = EvalSnapshot(
        case_id=case_id,
        case_version="1",
        run_id=f"run_{case_id}",
        case_state={"case_id": case_id},
        trace=snapshot_trace or {},
        events=snapshot_events or [],
    )
    result = EvalResult(
        case_id=case_id,
        case_version="1",
        run_id=snapshot.run_id,
        scorer_version="test_v1",
        passed=passed,
        business_passed=business_passed,
        score=score,
        first_failed_stage="reasoning" if not passed else "",
        checks=[
            ScoreCheck(
                id=failed_check,
                stage="reasoning",
                points=10,
                earned=10 if passed else 0,
                passed=passed,
                core=True,
                detail=f"{case_id} detail",
            )
        ],
        vetoes=[EvalVeto(code=veto, detail=f"{case_id} veto")] if veto else [],
        engineering=engineering
        if engineering is not None
        else {"provider_calls": provider_calls, "api_total_tokens": provider_calls * 100, "duration_ms": 50},
    )
    snapshot_path = run_dir / "snapshot.json"
    score_path = run_dir / "score.json"
    report_path = run_dir / "eval_report.md"
    snapshot_path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    score_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    report_path.write_text("# 单案例报告\n", encoding="utf-8")
    return BusinessEvalPaths(run_dir, snapshot_path, score_path, report_path)


def test_discovery_is_stable_and_filters_suite_without_oracle(tmp_path: Path) -> None:
    root = tmp_path / "cases"
    holdout = _write_case(root, "z_holdout", "composite_holdout")
    dev = _write_case(root, "a_dev", "atomic_dev")

    assert benchmark.discover_business_cases(root) == [dev.resolve(), holdout.resolve()]
    assert benchmark.discover_business_cases(root, suites=["composite_holdout"]) == [holdout.resolve()]


@pytest.mark.asyncio
async def test_live_benchmark_is_sequential_strict_and_tunes_only_dev(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases_root = tmp_path / "cases"
    dev = _write_case(cases_root, "dev_failure", "atomic_dev")
    holdout = _write_case(cases_root, "holdout_failure", "composite_holdout")
    active = False
    order: list[str] = []

    async def fake_run(case_dir: Path, *, output_root: Path, runtime_factory: Any = None) -> BusinessEvalPaths:
        nonlocal active
        assert runtime_factory is None
        assert active is False
        active = True
        await asyncio.sleep(0)
        order.append(case_dir.name)
        active = False
        return _write_scored_run(
            output_root,
            case_dir.name,
            passed=False,
            score=80 if case_dir == dev else 70,
            failed_check="dev.reasoning" if case_dir == dev else "holdout.reasoning",
            veto="DEV_VETO" if case_dir == dev else "HOLDOUT_VETO",
            provider_calls=1 if case_dir == dev else 2,
        )

    monkeypatch.setattr(benchmark, "run_business_eval", fake_run)
    paths = await benchmark.run_business_benchmark(
        [dev, holdout],
        output_dir=tmp_path / "benchmark_run",
    )
    summary = json.loads(paths.summary.read_text(encoding="utf-8"))
    report = paths.report.read_text(encoding="utf-8")

    assert order == ["dev_failure", "holdout_failure"]
    assert summary["strict_pass"] is False
    assert summary["score_mean"] == 75
    assert summary["core_checks"] == {"passed": 0, "total": 2, "pass_rate": 0.0}
    assert summary["outcomes"] == {
        "communication": {"passed": 0, "total": 2, "pass_rate": 0.0},
        "report": {"passed": 0, "total": 2, "pass_rate": 0.0},
        "runtime_completed": {"passed": 2, "total": 2, "pass_rate": 1.0},
        "target_truth": {"passed": 0, "total": 2, "pass_rate": 0.0},
    }
    assert summary["engineering_totals"]["provider_calls"] == 3
    assert summary["veto_counts"] == {"DEV_VETO": 1, "HOLDOUT_VETO": 1}
    assert {item["id"] for item in summary["repair_queue"]} == {"DEV_VETO", "dev.reasoning"}
    assert "dev.reasoning" in report
    assert "holdout.reasoning" not in report
    assert "holdout 结果不生成定向调优建议" in report


def test_snapshot_root_rescores_without_running_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases_root = tmp_path / "cases"
    _write_case(cases_root, "case_a", "atomic_dev")
    _write_case(cases_root, "case_b", "composite_holdout")
    snapshot_root = tmp_path / "saved"
    snapshot_paths: list[Path] = []
    for case_id in ("case_b", "case_a"):
        path = snapshot_root / case_id / "run" / "snapshot.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            EvalSnapshot(
                case_id=case_id,
                case_version="1",
                run_id=f"run_{case_id}",
                case_state={"case_id": case_id},
            ).model_dump_json(indent=2),
            encoding="utf-8",
        )
        snapshot_paths.append(path)
    rescored: list[str] = []

    def fake_score(snapshot_path: Path, *, case_dir: Path | None = None) -> BusinessEvalPaths:
        snapshot = EvalSnapshot.model_validate_json(snapshot_path.read_text(encoding="utf-8"))
        assert case_dir == (cases_root / snapshot.case_id).resolve()
        rescored.append(snapshot.case_id)
        return _write_scored_run(
            snapshot_path.parents[2],
            snapshot.case_id,
            passed=True,
            score=100,
            failed_check="reasoning.ok",
            veto="",
            provider_calls=0,
        )

    monkeypatch.setattr(benchmark, "score_snapshot", fake_score)
    monkeypatch.setattr(
        benchmark,
        "run_business_eval",
        lambda *_args, **_kwargs: pytest.fail("snapshot rescore must not run an agent"),
    )

    paths = benchmark.rescore_business_benchmark(
        snapshot_root,
        cases_root=cases_root,
        output_dir=tmp_path / "rescored_summary",
    )
    summary = json.loads(paths.summary.read_text(encoding="utf-8"))

    assert rescored == ["case_a", "case_b"]
    assert summary["strict_pass"] is True
    assert summary["case_count"] == 2
    assert paths.report.is_file()


def _telemetry(tokens: int, latency: int, *, ttft: int | None = None) -> tuple[dict, dict]:
    prompt = tokens - 10
    usage = {"prompt_tokens": prompt, "completion_tokens": 10, "cached_tokens": prompt // 4, "total_tokens": tokens}
    event = {"kind": "provider_call", "payload": {"role": "executor", "usage": usage}}
    call = {"role": "executor", "latency_ms": latency, **usage}
    if ttft is not None:
        call["ttft_ms"] = ttft
    return event, call


def test_efficiency_uses_canonical_events_and_preserves_raw_engineering(tmp_path: Path) -> None:
    case = _write_case(tmp_path / "cases", "metrics", "atomic_dev")
    event_a, call_a = _telemetry(120, 200, ttft=20)
    event_b, call_b = _telemetry(330, 100)
    event_a["payload"]["usage"]["reasoning_tokens"] = 5
    event_b["payload"]["usage"]["reasoning_tokens"] = 7
    event_a["payload"]["role"] = call_a["role"] = "task_compiler"
    raw = {
        # Deliberately stale: efficiency must replay the snapshot observations.
        "provider_calls": 99,
        "role_calls": 88,
        "api_prompt_tokens": 999,
        "api_completion_tokens": 999,
        "api_cached_tokens": 999,
        "api_total_tokens": 999,
        "duration_ms": 500,
        "tool_calls": 4,
        "tool_error_calls": 1,
        "raw_marker": "kept",
    }
    run = _write_scored_run(
        tmp_path / "runs", "metrics", passed=True, business_passed=True, score=87,
        failed_check="reasoning.ok", veto="", provider_calls=2, engineering=raw,
        snapshot_events=[event_a, event_b],
        snapshot_trace={"step_count": 3, "duration_ms": 500, "observability": {
            "model_metrics": {"call_count": 2, "calls": [call_a, call_b]},
            "compiler_run": {"retry_count": 2},
        }},
    )
    summary = benchmark.summarize_business_results(
        [run], case_dirs={"metrics": case}, run_dir=tmp_path / "runs"
    )
    row = summary["case_runs"][0]
    metrics = row["efficiency"]

    assert row["engineering"] == raw
    assert metrics["tokens"] == {
        "input_tokens": 430,
        "output_tokens": 20,
        "reasoning_tokens": 12,
        "cached_tokens": 107,
        "total_tokens": 450,
    }
    assert metrics["execution"]["provider_calls"] == 2
    assert metrics["execution"]["model_role_calls"] == 2
    assert metrics["execution"]["agent_turns"] == 3
    assert metrics["execution"]["tool_calls"] == 4
    assert metrics["execution"]["successful_tool_calls"] == 3
    assert metrics["execution"]["tool_error_count"] == 1
    assert metrics["execution"]["compiler_retry_count"] == 2
    assert metrics["execution"]["max_turns_hits"] == 0
    assert metrics["latency"] == {
        "e2e_duration_ms": 500, "model_call_p50_ms": 100, "model_call_p95_ms": 200,
        "ttft_ms": 20, "ttft_p95_ms": 20,
        "coverage": {
            "model_latency": {"observed": 2, "total": 2},
            "ttft": {"observed": 1, "total": 2},
        },
    }
    assert metrics["context"] == {"peak_context_tokens": 320, "cache_hit_ratio": round(107 / 430, 4)}
    assert metrics["by_role"]["task_compiler"]["latency_p50_ms"] == 200
    assert metrics["by_role"]["executor"]["ttft_p50_ms"] is None
    assert "_model_latency_samples_ms" not in json.dumps(metrics)
    assert "_ttft_samples_ms" not in json.dumps(metrics)
    assert summary["strict_pass"] is True and summary["score_mean"] == 87
    report = benchmark.render_business_benchmark(summary)
    assert all(label in report for label in ("Calls", "Tokens", "TTFT", "效率指标不与业务分合成"))
    assert "task_compiler" in report
    assert "pass^" not in report.casefold()


def test_missing_provider_telemetry_stays_unknown(tmp_path: Path) -> None:
    case = _write_case(tmp_path / "cases", "legacy", "atomic_dev")
    run = _write_scored_run(
        tmp_path / "runs",
        "legacy",
        passed=False,
        score=0,
        failed_check="reasoning.missing",
        veto="FAILED",
        provider_calls=0,
        engineering={"provider_calls": 0, "role_calls": 1, "api_total_tokens": 0},
        snapshot_events=[{"kind": "tool_started", "name": "read_source"}],
        snapshot_trace={
            "observability": {
                "model_metrics": {"calls": [{"role": "executor", "latency_ms": 50}]}
            }
        },
    )
    summary = benchmark.summarize_business_results(
        [run], case_dirs={"legacy": case}, run_dir=tmp_path / "runs"
    )
    metrics = summary["case_runs"][0]["efficiency"]

    assert metrics["execution"]["provider_calls"] is None
    assert metrics["tokens"]["total_tokens"] is None
    assert metrics["by_role"]["executor"]["total_tokens"] is None


def test_percentiles_null_coverage_and_success_adjusted_metrics(tmp_path: Path) -> None:
    cases = {name: _write_case(tmp_path / "cases", name, "atomic_dev") for name in ("pass", "fail")}
    runs = []
    for name, passed, tokens, duration, latency, ttft in (
        ("pass", True, 100, 1_000, 100, 10),
        ("fail", False, 300, 3_000, 900, None),
    ):
        event, call = _telemetry(tokens, latency, ttft=ttft)
        if name == "fail":
            event["payload"]["usage"].pop("cached_tokens")
        runs.append(_write_scored_run(
            tmp_path / "runs", name, passed=passed, business_passed=passed,
            score=100 if passed else 50, failed_check="reasoning.truth",
            veto="" if passed else "FAILED", provider_calls=1,
            snapshot_events=[event], snapshot_trace={"duration_ms": duration, "observability": {
                "model_metrics": {"call_count": 1, "calls": [call]}}}, engineering={},
        ))
    summary = benchmark.summarize_business_results(runs, case_dirs=cases, run_dir=tmp_path / "runs")
    dist = summary["engineering_distribution"]

    assert dist["total_tokens"] == {"p50": 100, "p95": 300, "observed_runs": 2, "total_runs": 2}
    assert dist["model_call_latency_ms"] == {"p50": 100, "p95": 900, "observed_calls": 2, "total_calls": 2}
    assert dist["ttft_ms"] == {"p50": 10, "p95": 10, "observed_calls": 1, "total_calls": 2}
    fail_row = next(row for row in summary["case_runs"] if row["case_id"] == "fail")
    assert fail_row["efficiency"]["tokens"]["cached_tokens"] is None
    fail_index = 0 if summary["case_runs"][0]["case_id"] == "fail" else 1
    assert summary["case_runs"][fail_index]["efficiency"]["latency"]["ttft_ms"] is None
    assert summary["engineering_efficiency"] == {
        "business_pass_count": 1, "tokens_per_business_pass": 400,
        "latency_ms_per_business_pass": 4_000,
    }
    no_pass = benchmark.summarize_business_results(
        [runs[1]], case_dirs={"fail": cases["fail"]}, run_dir=tmp_path / "runs"
    )
    assert no_pass["engineering_efficiency"]["tokens_per_business_pass"] is None
    assert no_pass["engineering_efficiency"]["latency_ms_per_business_pass"] is None
    assert summary["engineering_distribution"] == benchmark.summarize_business_results(
        runs, case_dirs=cases, run_dir=tmp_path / "runs"
    )["engineering_distribution"]
