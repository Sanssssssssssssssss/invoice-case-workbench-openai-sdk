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
) -> BusinessEvalPaths:
    run_dir = output_root / case_id / "run_1"
    run_dir.mkdir(parents=True, exist_ok=True)
    snapshot = EvalSnapshot(
        case_id=case_id,
        case_version="1",
        run_id=f"run_{case_id}",
        case_state={"case_id": case_id},
    )
    result = EvalResult(
        case_id=case_id,
        case_version="1",
        run_id=snapshot.run_id,
        scorer_version="test_v1",
        passed=passed,
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
        engineering={"provider_calls": provider_calls, "api_total_tokens": provider_calls * 100, "duration_ms": 50},
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
