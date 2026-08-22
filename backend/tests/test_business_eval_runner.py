from __future__ import annotations

import hashlib
import json
import shutil
from decimal import Decimal
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from typing import Any

import pytest

from app.evals.business.models import EvalResult, EvalSnapshot, ScoreCheck
from app.evals.business.runner import run_business_eval, score_revision_id, score_snapshot
from app.state.schemas import AgentTurnResponse, CaseState
from app.state.session_repository import SessionRepository


def _write_case(root: Path) -> Path:
    case_dir = root / "invoice_eval"
    attachments = case_dir / "attachments"
    attachments.mkdir(parents=True)
    pdf = attachments / "invoice.pdf"
    pdf.write_bytes(b"%PDF-1.4\nunit-test\n%%EOF")
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    (case_dir / "case.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "case_id": "invoice_eval",
                "case_version": "1",
                "title": "中文测试案例",
                "suite": "atomic_dev",
                "business_holes": ["invoice_arithmetic"],
                "user_message": "请审核这张发票并生成中文报告。",
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
    (case_dir / "oracle.json").write_text(
        json.dumps(
            {
                "schema_version": "2",
                "case_id": "invoice_eval",
                "oracle_version": "2",
                "sentinel": "NEVER_IN_RUNTIME",
                "facts": [
                    {
                        "id": "amount_a",
                        "origin": "source",
                        "kind": "decimal",
                        "value": "5.00",
                        "source_role": "invoice",
                        "source_quote": "A 5.00",
                    },
                    {
                        "id": "amount_b",
                        "origin": "source",
                        "kind": "decimal",
                        "value": "7.00",
                        "source_role": "invoice",
                        "source_quote": "B 7.00",
                    },
                    {
                        "id": "total",
                        "origin": "derived",
                        "kind": "decimal",
                        "value": "12.00",
                    },
                ],
                "intent": {
                    "objective": {"all_of": [["invoice"], ["calculate"]]},
                    "required_milestone_ids": ["total_math"],
                },
                "milestones": [
                    {
                        "id": "total_math",
                        "statement_meaning": {"all_of": [["sum"], ["total"]]},
                        "expected_status": "SUPPORTED",
                        "fact_ids": ["amount_a", "amount_b", "total"],
                        "relation_ids": ["sum_total"],
                    }
                ],
                "relations": [
                    {
                        "id": "sum_total",
                        "operation": "sum",
                        "input_fact_ids": ["amount_a", "amount_b"],
                        "output_fact_id": "total",
                    }
                ],
                "epistemic_boundaries": [],
                "requirement": {
                    "requirement_id": "invoice_calculation_valid",
                    "decision_status": "CONTRADICTED",
                    "projected_status": "conflict",
                    "blocking_obligations": False,
                },
                "communication": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return case_dir


def _result(snapshot: EvalSnapshot) -> EvalResult:
    return EvalResult(
        case_id=snapshot.case_id,
        case_version=snapshot.case_version,
        run_id=snapshot.run_id,
        scorer_version="test",
        passed=True,
        score=Decimal("100"),
        checks=[],
    )


class _FakeRuntime:
    def __init__(self, store: Any, approval_tools: list[str]) -> None:
        self.store = store
        self.approval_tools = list(approval_tools)
        self.resume_calls = 0
        self.repository = SessionRepository(store)
        self.runner = SimpleNamespace(
            settings=SimpleNamespace(llm_provider="fake", llm_model="fake-model"),
        )

    async def run_turn_streamed(self, request: Any, *, run_id: str, event_sink: Any) -> AgentTurnResponse:
        self.repository.append_user_turn(
            request.case_id,
            request.message,
            [item.model_dump() for item in request.attachments],
            run_id,
        )
        assert request.attachments[0].path == "attachments/invoice.pdf"
        assert "NEVER_IN_RUNTIME" not in request.message
        event_sink("model_started", {"role": "planner"}, summary="开始")
        return self._response(request.case_id, run_id, self.approval_tools[0])

    async def resume_approval_streamed(
        self,
        case_id: str,
        run_id: str,
        approved: bool,
        reason: str,
        *,
        event_sink: Any,
    ) -> AgentTurnResponse:
        assert approved is True
        assert "本地报告写入" in reason
        self.resume_calls += 1
        tool = self.approval_tools[self.resume_calls] if self.resume_calls < len(self.approval_tools) else ""
        if tool:
            return self._response(case_id, run_id, tool)

        case_root = self.store.ensure_case_dirs(case_id)
        (case_root / "reports" / "report.md").write_text("# 中文报告\n\n金额存在冲突。", encoding="utf-8")
        (case_root / "reports" / "report.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
        trace = {
            "run_id": run_id,
            "status": "completed",
            "final_answer": "已生成中文报告，金额存在冲突。",
        }
        (case_root / "traces" / f"{run_id}.json").write_text(json.dumps(trace), encoding="utf-8")
        run_trace = case_root / "traces" / run_id
        run_trace.mkdir(parents=True, exist_ok=True)
        (run_trace / "events.jsonl").write_text(
            json.dumps(
                {
                    "kind": "provider_call",
                    "payload": {
                        "role": "planner",
                        "model": "fake-model",
                        "prompt_version": "planner_test_v1",
                        "system_prompt": "planner system prompt",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (run_trace / "deepseek_calls.txt").write_text("调用 001 | planner | OK\n", encoding="utf-8")
        session = self.repository.load_session(case_id)
        turn_id = session["turns"][0]["turn_id"]
        self.repository.append_assistant_turn(case_id, turn_id, trace["final_answer"], run_id)
        return AgentTurnResponse(
            case_id=case_id,
            reply=trace["final_answer"],
            case_state=CaseState(case_id=case_id),
            trace=trace,
        )

    def _response(self, case_id: str, run_id: str, tool: str) -> AgentTurnResponse:
        return AgentTurnResponse(
            case_id=case_id,
            reply="等待批准",
            case_state=CaseState(case_id=case_id),
            trace={
                "run_id": run_id,
                "status": "waiting_approval",
                "interrupts": [{"type": "tool_approval", "tool": tool}],
            },
        )


@pytest.mark.asyncio
async def test_runner_captures_isolated_streamed_run_and_two_allowed_approvals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_dir = _write_case(tmp_path)
    runtime_box: list[_FakeRuntime] = []

    def factory(store: Any) -> _FakeRuntime:
        runtime = _FakeRuntime(store, ["write_case_file", "render_pdf"])
        runtime_box.append(runtime)
        return runtime

    monkeypatch.setattr("app.evals.business.scorer.score_business_eval", lambda _c, _o, snapshot: _result(snapshot))
    monkeypatch.setattr("app.evals.business.report.render_eval_report", lambda *_args: "# Eval 结果\n\n通过。")

    paths = await run_business_eval(case_dir, output_root=tmp_path / "output", runtime_factory=factory)
    snapshot = EvalSnapshot.model_validate_json(paths.snapshot.read_text(encoding="utf-8"))

    assert runtime_box[0].resume_calls == 2
    assert snapshot.approved_tools == ["write_case_file", "render_pdf"]
    assert snapshot.runtime_error == ""
    assert snapshot.provider == "fake"
    assert snapshot.model == "fake-model"
    assert snapshot.policy_version == "policy_test_v1"
    assert snapshot.conversation[0]["role"] == "user"
    assert snapshot.conversation[0]["content"].startswith("请审核")
    assert snapshot.conversation[-1]["role"] == "assistant"
    assert "金额存在冲突" in snapshot.conversation[-1]["content"]
    assert {item.kind for item in snapshot.reports} == {"markdown", "pdf"}
    assert snapshot.events[0]["kind"] == "provider_call"
    assert snapshot.transcript_path.endswith("deepseek_calls.txt")
    assert (paths.run_dir / "w" / "invoice_eval" / "case_state.json").is_file()
    assert (paths.run_dir / "s" / "sessions.sqlite").is_file()
    assert paths.report.read_text(encoding="utf-8").startswith("# Eval 结果")
    assert paths.revision is not None
    assert paths.score.parent.parent == paths.run_dir / "scores"
    assert not (paths.run_dir / "score.json").exists()
    revision = json.loads(paths.revision.read_text(encoding="utf-8"))
    assert revision["revision_id"] == score_revision_id(revision)
    assert "created_at" not in revision
    assert revision["artifacts"]["score"]["sha256"] == hashlib.sha256(
        paths.score.read_bytes()
    ).hexdigest()
    assert revision["artifacts"]["report"]["sha256"] == hashlib.sha256(
        paths.report.read_bytes()
    ).hexdigest()
    manifest_text = (paths.run_dir / "run_manifest.json").read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert "NEVER_IN_RUNTIME" not in manifest_text
    assert manifest["snapshot"]["sha256"] == hashlib.sha256(paths.snapshot.read_bytes()).hexdigest()
    assert manifest["code"]["git_head"] == snapshot.agent_commit
    assert manifest["code"]["scope"] == ["backend/app", "policies"]
    assert len(manifest["code"]["fingerprint"]) == 64
    assert manifest["provider_prompts"] == [
        {
            "calls": 1,
            "model": "fake-model",
            "prompt_sha256": hashlib.sha256(b"planner system prompt").hexdigest(),
            "prompt_version": "planner_test_v1",
            "role": "planner",
        }
    ]


@pytest.mark.asyncio
async def test_runner_layout_stays_below_windows_max_path_for_regression_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_dir = _write_case(tmp_path)
    stamp = "20260820T223544_791710Z"
    monkeypatch.setattr("app.evals.business.runner._timestamp_id", lambda: stamp)
    monkeypatch.setattr("app.evals.business.scorer.score_business_eval", lambda _c, _o, snapshot: _result(snapshot))
    monkeypatch.setattr("app.evals.business.report.render_eval_report", lambda *_args: "# Eval")

    paths = await run_business_eval(
        case_dir,
        output_root=tmp_path / "output",
        runtime_factory=lambda store: _FakeRuntime(store, ["write_case_file", "render_pdf"]),
    )
    snapshot = EvalSnapshot.model_validate_json(paths.snapshot.read_text(encoding="utf-8"))

    assert paths.run_dir.relative_to((tmp_path / "output").resolve()).parts == (stamp,)
    assert snapshot.case_id == "invoice_eval"
    assert snapshot.run_id == f"run_eval_{stamp}"

    regression_case_id = "reverse_charge_arithmetic_supported_0020"
    artifact = (
        PureWindowsPath(r"E:\GPTProject2\erp-openai\output\b10_live1")
        / stamp
        / "w"
        / regression_case_id
        / "traces"
        / "artifacts"
        / snapshot.run_id
        / "art_001_attachment_batch_read_attachment.json"
    )
    assert len(str(artifact)) < 260
    assert str(artifact).count(regression_case_id) == 1


@pytest.mark.asyncio
async def test_runner_fails_closed_for_undeclared_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_dir = _write_case(tmp_path)
    runtime_box: list[_FakeRuntime] = []

    def factory(store: Any) -> _FakeRuntime:
        runtime = _FakeRuntime(store, ["list_case_files"])
        runtime_box.append(runtime)
        return runtime

    monkeypatch.setattr("app.evals.business.scorer.score_business_eval", lambda _c, _o, snapshot: _result(snapshot))
    monkeypatch.setattr("app.evals.business.report.render_eval_report", lambda *_args: "# Eval")

    paths = await run_business_eval(case_dir, output_root=tmp_path / "output", runtime_factory=factory)
    snapshot = EvalSnapshot.model_validate_json(paths.snapshot.read_text(encoding="utf-8"))

    assert runtime_box[0].resume_calls == 0
    assert snapshot.approved_tools == []
    assert snapshot.runtime_error == "ApprovalDenied: tool is not auto-approved: list_case_files"


@pytest.mark.asyncio
async def test_runner_rejects_tampered_input_before_runtime_or_oracle(
    tmp_path: Path,
) -> None:
    case_dir = _write_case(tmp_path)
    (case_dir / "attachments" / "invoice.pdf").write_bytes(b"tampered")
    called = False

    def factory(_store: Any) -> _FakeRuntime:
        nonlocal called
        called = True
        raise AssertionError("must not construct runtime")

    with pytest.raises(ValueError, match="attachment hash mismatch"):
        await run_business_eval(case_dir, output_root=tmp_path / "output", runtime_factory=factory)
    assert called is False


def test_snapshot_can_be_rescored_without_constructing_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_dir = _write_case(tmp_path)
    run_dir = tmp_path / "existing"
    run_dir.mkdir()
    snapshot = EvalSnapshot(
        case_id="invoice_eval",
        case_version="1",
        run_id="run_existing",
        policy_version="policy_test_v1",
        case_state={"case_id": "invoice_eval"},
    )
    snapshot_path = run_dir / "snapshot.json"
    snapshot_path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    legacy_score = run_dir / "score.json"
    legacy_report = run_dir / "eval_report.md"
    legacy_score.write_text("legacy score\n", encoding="utf-8")
    legacy_report.write_text("legacy report\n", encoding="utf-8")
    original_snapshot = snapshot_path.read_bytes()
    calls = 0

    def scorer(_case: Any, _oracle: Any, observed: EvalSnapshot) -> EvalResult:
        nonlocal calls
        calls += 1
        result = _result(observed)
        expected = {"a": 1, "b": 2} if calls % 2 else {"b": 2, "a": 1}
        result.checks = [
            ScoreCheck(
                id="canonical.expected",
                stage="reasoning",
                points=Decimal("1"),
                earned=Decimal("1"),
                passed=True,
                expected=expected,
            )
        ]
        return result

    monkeypatch.setattr("app.evals.business.scorer.score_business_eval", scorer)
    monkeypatch.setattr("app.evals.business.report.render_eval_report", lambda *_args: "# 重评报告")

    paths = score_snapshot(snapshot_path, case_dir=case_dir)
    first_revision = paths.revision.read_bytes() if paths.revision else b""
    first_score = paths.score.read_bytes()
    first_report = paths.report.read_bytes()
    repeated = score_snapshot(snapshot_path, case_dir=case_dir)
    shutil.rmtree(paths.score.parent)
    rebuilt = score_snapshot(snapshot_path, case_dir=case_dir)

    assert calls == 3
    assert paths.score.is_file()
    assert paths.report.read_text(encoding="utf-8").startswith("# 重评报告")
    assert paths.revision is not None
    assert repeated.revision == paths.revision
    assert rebuilt.revision is not None
    assert rebuilt.revision.read_bytes() == first_revision
    assert rebuilt.score.read_bytes() == first_score
    assert rebuilt.report.read_bytes() == first_report
    assert snapshot_path.read_bytes() == original_snapshot
    assert legacy_score.read_text(encoding="utf-8") == "legacy score\n"
    assert legacy_report.read_text(encoding="utf-8") == "legacy report\n"


def test_score_revision_conflict_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_dir = _write_case(tmp_path)
    run_dir = tmp_path / "existing"
    run_dir.mkdir()
    snapshot = EvalSnapshot(
        case_id="invoice_eval",
        case_version="1",
        run_id="run_existing",
        policy_version="policy_test_v1",
        case_state={"case_id": "invoice_eval"},
    )
    snapshot_path = run_dir / "snapshot.json"
    snapshot_path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    monkeypatch.setattr("app.evals.business.scorer.score_business_eval", lambda _c, _o, observed: _result(observed))
    monkeypatch.setattr("app.evals.business.report.render_eval_report", lambda *_args: "# stable")

    paths = score_snapshot(snapshot_path, case_dir=case_dir)
    paths.score.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="score revision conflict"):
        score_snapshot(snapshot_path, case_dir=case_dir)


@pytest.mark.asyncio
async def test_run_manifest_detects_snapshot_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_dir = _write_case(tmp_path)
    monkeypatch.setattr("app.evals.business.scorer.score_business_eval", lambda _c, _o, snapshot: _result(snapshot))
    monkeypatch.setattr("app.evals.business.report.render_eval_report", lambda *_args: "# Eval")
    paths = await run_business_eval(
        case_dir,
        output_root=tmp_path / "output",
        runtime_factory=lambda store: _FakeRuntime(store, ["write_case_file", "render_pdf"]),
    )
    paths.snapshot.write_text(paths.snapshot.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="run manifest input hash mismatch"):
        score_snapshot(paths.snapshot, case_dir=case_dir)


@pytest.mark.asyncio
async def test_runner_fails_if_case_changes_during_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_dir = _write_case(tmp_path)

    class MutatingRuntime(_FakeRuntime):
        async def run_turn_streamed(
            self,
            request: Any,
            *,
            run_id: str,
            event_sink: Any,
        ) -> AgentTurnResponse:
            response = await super().run_turn_streamed(
                request,
                run_id=run_id,
                event_sink=event_sink,
            )
            case_path = case_dir / "case.json"
            case_path.write_bytes(case_path.read_bytes() + b"\n")
            return response

    monkeypatch.setattr("app.evals.business.scorer.score_business_eval", lambda _c, _o, snapshot: _result(snapshot))
    monkeypatch.setattr("app.evals.business.report.render_eval_report", lambda *_args: "# Eval")
    output_root = tmp_path / "output"

    with pytest.raises(RuntimeError, match="case.json changed during eval run"):
        await run_business_eval(
            case_dir,
            output_root=output_root,
            runtime_factory=lambda store: MutatingRuntime(
                store,
                ["write_case_file", "render_pdf"],
            ),
        )
    assert not list(output_root.rglob("snapshot.json"))
