from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from agents.exceptions import MaxTurnsExceeded

from app.compiler_runtime import (
    CheckAssessment,
    Claim,
    EvidenceIR,
    ProofNode,
    ProofPlan,
    ReviewArtifact,
    compile_review_artifact,
)
from app.compiler_runtime.policy import policy_excerpt_for, policy_hash
from app.compiler_runtime.signatures import proof_signature_hash_for
from app.config import get_settings
from app.harness import HarnessRuntime
from app.llm import ModelCallRecord
from app.runtime.policy_gate import _report_requested as _policy_report_requested
from app.runtime.turn_runner import AgentRuntime, ManagerRunOutcome, SdkManagerRunner, TurnRunner, _canonical_report_summary, _latest_observation_index, _report_requested_message, _runtime_final_answer
from app.state.attachment_manifest import save_attachment_manifest
from app.state.case_store import CaseStore
from app.state.schemas import AgentTurnRequest, AgentTurnResponse, Attachment, CaseState, EvidenceItem, Requirement, SupervisorDecision


def test_observation_index_tracks_latest_reviewer_for_patch_ordering() -> None:
    state = SimpleNamespace(observations=[
        {"kind": "role", "name": "evidence_reviewer"},
        {"kind": "role", "name": "case_patch_writer"},
        {"kind": "tool", "name": "write_case_patch"},
        {"kind": "role", "name": "evidence_reviewer", "reviewer_mode": "repair"},
    ])

    assert _latest_observation_index(state, kind="role", name="evidence_reviewer") == 3
    assert _latest_observation_index(state, kind="role", name="case_patch_writer") == 1


def test_canonical_report_summary_extracts_business_paragraph_only() -> None:
    markdown = """# 报告

### 摘要结论

票面总额 **13,156.92 EUR**，重算总额 13,563.84 EUR，差额 406.92 EUR。

## 证明结果

后续内容。
"""

    assert _canonical_report_summary(markdown) == (
        "票面总额 13,156.92 EUR，重算总额 13,563.84 EUR，差额 406.92 EUR。"
    )
    assert _canonical_report_summary("# 无标准摘要\n\n正文") == ""


class ScriptedManagerRunner:
    def __init__(self, decisions: list[dict[str, Any]]) -> None:
        self.decisions = list(decisions)
        self.payload: dict[str, Any] = {}
        self.pending: dict[str, dict[str, Any]] = {}

    def run(
        self,
        *,
        runner: TurnRunner,
        request: AgentTurnRequest,
        state,
        planner_context: dict[str, Any],
        manager_input: dict[str, Any],
    ) -> ManagerRunOutcome:
        self.payload = manager_input
        decision = self.decisions.pop(0) if self.decisions else {"action": "final_answer", "final_answer": "已完成。"}
        action = decision.get("action")
        if action in {"call_tool", "delegate_agent", "write_case_patch"}:
            target = "write_case_patch" if action == "write_case_patch" else str(decision.get("target") or "")
            result = runner.invoke_manager_tool(
                state=state,
                request=request,
                planner_context=planner_context,
                name=target,
                payload=dict(decision.get("input") or {}),
            )
            if result.get("status") == "approval_required":
                self.pending[state.run_id] = decision
                return ManagerRunOutcome(
                    interruptions=[{"tool": target, "input": dict(decision.get("input") or {}), "risk_level": "read"}],
                    sdk_state="fake_pending_state",
                )
            return self.run(runner=runner, request=request, state=state, planner_context=planner_context, manager_input=manager_input)
        return ManagerRunOutcome(final_output=str(decision.get("final_answer") or ""))

    def resume(
        self,
        *,
        runner: TurnRunner,
        request: AgentTurnRequest,
        state,
        sdk_state: str,
        approved: bool,
        reason: str = "",
    ) -> ManagerRunOutcome:
        _ = sdk_state
        _ = reason
        planner_context = runner.context_assembler.build_planner_context(request, state)
        manager_input = runner._manager_input(request, state, planner_context)
        if approved and state.run_id in self.pending:
            decision = self.pending.pop(state.run_id)
            target = str(decision.get("target") or "")
            runner.invoke_manager_tool(
                state=state,
                request=request,
                planner_context=planner_context,
                name=target,
                payload=dict(decision.get("input") or {}),
            )
        elif not approved:
            self.pending.pop(state.run_id, None)
        return self.run(runner=runner, request=request, state=state, planner_context=planner_context, manager_input=manager_input)


class ReportSdkStateMustNotResumeManager(ScriptedManagerRunner):
    def __init__(self) -> None:
        super().__init__([])
        self.run_calls = 0
        self.resume_calls = 0

    def run(self, **_kwargs: Any) -> ManagerRunOutcome:
        self.run_calls += 1
        raise AssertionError("canonical report continuation must not call Manager")

    async def run_streamed(self, **_kwargs: Any) -> ManagerRunOutcome:
        self.run_calls += 1
        raise AssertionError("canonical report continuation must not call streamed Manager")

    def resume(self, **_kwargs: Any) -> ManagerRunOutcome:
        self.resume_calls += 1
        raise AssertionError("canonical report approval must not resume old Manager SDK state")

    async def resume_streamed(self, **_kwargs: Any) -> ManagerRunOutcome:
        self.resume_calls += 1
        raise AssertionError("canonical report approval must not resume old streamed Manager SDK state")


class ReportWriterThenMaxTurnsManager(ScriptedManagerRunner):
    def __init__(self) -> None:
        super().__init__([])
        self.run_calls = 0

    def _write_then_fail(self, **kwargs: Any) -> ManagerRunOutcome:
        self.run_calls += 1
        runner = kwargs["runner"]
        result = runner.invoke_manager_tool(
            state=kwargs["state"],
            request=kwargs["request"],
            planner_context=kwargs["planner_context"],
            name="report_writer",
            payload={},
        )
        assert result["status"] == "success"
        raise MaxTurnsExceeded("Max turns (11) exceeded")

    def run(self, **kwargs: Any) -> ManagerRunOutcome:
        return self._write_then_fail(**kwargs)

    async def run_streamed(self, **kwargs: Any) -> ManagerRunOutcome:
        return self._write_then_fail(**kwargs)


class MaxTurnsWithoutWriterManager(ScriptedManagerRunner):
    def __init__(self) -> None:
        super().__init__([])

    def run(self, **_kwargs: Any) -> ManagerRunOutcome:
        raise MaxTurnsExceeded("Max turns exceeded before report_writer")

    async def run_streamed(self, **_kwargs: Any) -> ManagerRunOutcome:
        raise MaxTurnsExceeded("Max turns exceeded before report_writer")


def _runtime(tmp_path, monkeypatch, manager: ScriptedManagerRunner) -> AgentRuntime:
    monkeypatch.setenv("INVOICE_AGENT_WORKSPACE_ROOT", str(tmp_path / "cases"))
    monkeypatch.setenv("INVOICE_AGENT_STORAGE_ROOT", str(tmp_path / "storage"))
    get_settings.cache_clear()
    return AgentRuntime(store=CaseStore(tmp_path / "cases"), manager_runner=manager)


def _runtime_artifact(
    requirement_id: str,
    status: str,
    *,
    missing_fact: str = "",
    source_id: str = "ev_proof",
    source_text: str = "",
) -> ReviewArtifact:
    source_text = source_text or f"Evidence for {requirement_id}"
    check_id = f"check.{requirement_id}"
    plan = ProofPlan(
        plan_id=f"plan.{requirement_id}",
        objective=f"Establish {requirement_id} from grounded case evidence.",
        active_requirement_ids=[requirement_id],
        roots={requirement_id: check_id},
        nodes=[ProofNode(
            id=check_id,
            kind="CHECK",
            statement=f"The available evidence supports {requirement_id}.",
            requirement_refs=[requirement_id],
        )],
    )
    claims = []
    if status != "NOT_FOUND":
        claims = [Claim(
            id=f"claim.{requirement_id}",
            subject=f"requirement:{requirement_id}",
            predicate="evidence_status",
            value=status.lower(),
            source_id=source_id,
            quote=source_text,
            locator="line 1",
            confidence="high",
        )]
    evidence_ir = EvidenceIR(
        source_ids=[source_id],
        source_fingerprints={source_id: hashlib.sha256(source_text.encode("utf-8")).hexdigest()},
        claims=claims,
    )
    assessment = CheckAssessment(
        check_id=check_id,
        status=status,
        claim_ids=[item.id for item in claims],
        source_ids=[source_id] if claims else [],
        examined_source_ids=[source_id],
        reason="fixture verifier assessment",
        missing_fact=missing_fact,
    )
    artifact = ReviewArtifact(
        plan=plan,
        plan_hash=plan.content_hash(),
        evidence_ir=evidence_ir,
        evidence_snapshot_hash=evidence_ir.content_hash(),
        assessments=[assessment],
        submitted_claim_refs={check_id: [item.id for item in claims]},
        proof_signature_hash=proof_signature_hash_for(plan.active_requirement_ids),
        policy_hash=policy_hash(policy_excerpt_for([requirement_id])),
        compiler_version="test",
        model="fixture",
    )
    return artifact.model_copy(update={"artifact_hash": artifact.content_hash()})


def _runtime_proof(
    requirement_id: str,
    status: str,
    *,
    missing_fact: str = "",
):
    return compile_review_artifact(
        _runtime_artifact(requirement_id, status, missing_fact=missing_fact)
    )


def _seed_ready_case(store: CaseStore, case_id: str) -> None:
    source_text = "Invoice INV-5001 remains payable\nOrder scope PO-5001\nTotal 12800.00 CNY\nInvoice total\nGross total"
    evidence_items = [
        EvidenceItem(
            id="ev_001",
            type="invoice",
            source="attachment",
            summary="Invoice INV-5001 amount 12800.00 CNY.",
            content=source_text,
            review_result={"should_accept": True},
            supports=[{"requirement": "invoice", "support_level": "full"}],
            metadata={
                "classification": "business_evidence",
                "original_ref": "attachments/originals/ev_001.md",
                "compiler_source_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
                "extracted_fields": {
                    "amount_total": {"value": "12800.00", "source_quote": "Total 12800.00 CNY", "source_locator": "invoice total", "confidence": "high"},
                    "basis": {"value": "invoice_total", "source_quote": "Invoice total", "source_locator": "invoice total", "confidence": "high"},
                    "tax_basis": {"value": "gross", "source_quote": "Gross total", "source_locator": "invoice total", "confidence": "high"},
                    "coverage": {"value": "full", "source_quote": "Invoice total", "source_locator": "invoice total", "confidence": "high"},
                },
            },
        ),
    ]
    evidence_items[0].supports[0].quoted_text = "Total 12800.00 CNY"
    source_texts = {
        "ev_001": source_text,
    }
    attachments = []
    for item in evidence_items:
        original_ref = item.metadata["original_ref"]
        path = store.resolve_case_path(case_id, original_ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source_texts[item.id], encoding="utf-8")
        attachments.append({
            "attachment_id": f"att_{item.id}",
            "name": f"{item.id}.md",
            "original_ref": original_ref,
            "status": "active",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "evidence_ids": [item.id],
        })
    save_attachment_manifest(store, case_id, {"attachments": attachments})
    artifact = _runtime_artifact(
        "invoice",
        "SUPPORTED",
        source_id="ev_001",
        source_text=source_text,
    )
    store.save(CaseState(
        case_id=case_id,
        status="ready_for_report",
        requirements=[
            Requirement(id="invoice", status="satisfied", evidence_ids=["ev_001"], kind="document"),
        ],
        evidence_items=evidence_items,
        review_artifact=artifact,
        compiled_proof=compile_review_artifact(artifact),
    ))


def _fake_report_writer(role: str, role_input: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
    assert role == "report_writer"
    assert set(role_input) == {"canonical_consumer_packet", "user_request"}
    assert role_input["canonical_consumer_packet"]["reportability"] == "FULL"
    assert "case_state" not in role_input
    assert "attachment_manifest" not in role_input
    return {"title": "INV-5001 审核报告", "markdown": "# INV-5001 审核报告\n\n五项核心材料均已满足。\n"}


def _persist_test_report(runner: TurnRunner, state: Any, markdown: str) -> None:
    runner.tool_runtime.call(
        state,
        "write_case_file",
        {"relative_path": "reports/final_report.md", "content": markdown},
        internal=True,
    )


def _checkpoint_sdk_report_write_approval(runtime: AgentRuntime, case_id: str) -> AgentTurnResponse:
    runner = runtime.runner
    request = AgentTurnRequest(case_id=case_id, message="final report and render PDF")
    state = runner.harness.begin_run(case_id, request.message, run_id=f"run_{case_id}")
    result = runner._call_specialist(  # noqa: SLF001
        state,
        request,
        {},
        SupervisorDecision(action="delegate_agent", target="report_writer", input={}),
    )
    assert result["status"] == "success"
    return runner._waiting_approval_response(  # noqa: SLF001
        request,
        state,
        "serialized_manager_sdk_state",
        [
            {
                "tool": "write_case_file",
                "input": {"content_ref": "last_role:report_writer.markdown"},
                "risk_level": "write",
            }
        ],
    )


def _resume_test_approval(
    runtime: AgentRuntime,
    case_id: str,
    response: AgentTurnResponse,
    *,
    approved: bool,
    streamed: bool,
) -> AgentTurnResponse:
    reason = "ok" if approved else "no"
    if streamed:
        return asyncio.run(
            runtime.resume_approval_streamed(
                case_id,
                response.trace["run_id"],
                approved=approved,
                reason=reason,
            )
        )
    return runtime.resume_approval(case_id, response.trace["run_id"], approved=approved, reason=reason)


def _run_report_manager_until_max_turns(
    runtime: AgentRuntime,
    case_id: str,
    *,
    streamed: bool,
    prior_write_observation: bool = False,
) -> AgentTurnResponse:
    request = AgentTurnRequest(case_id=case_id, message="生成最终报告并渲染 PDF")
    state = runtime.runner.harness.begin_run(case_id, request.message, run_id=f"run_{case_id}")
    # Reproduce an SDK Manager turn already in progress: report_writer completes
    # inside that turn, then the SDK reaches max_turns before requesting write approval.
    state.observability["_manager_final_rewrite_pending"] = True
    if prior_write_observation:
        state.observations.append({"kind": "tool", "name": "write_case_file"})
    if streamed:
        return asyncio.run(runtime.runner._run_until_final_streamed(request, state))  # noqa: SLF001
    return runtime.runner._run_until_final(request, state)  # noqa: SLF001


def _runtime_event_pairs(runtime: AgentRuntime, case_id: str, run_id: str) -> set[tuple[str, str]]:
    path = runtime.runner.store.resolve_case_path(case_id, f"traces/{run_id}/events.jsonl")
    return {
        (str(item.get("kind") or ""), str(item.get("name") or ""))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
        for item in [json.loads(line)]
    }


def test_report_writer_discards_manager_side_channel_and_hands_off_canonical_content_ref(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    runner = runtime.runner
    case_id = "case_report_context_boundary"
    _seed_ready_case(runner.store, case_id)
    request = AgentTurnRequest(case_id=case_id, message="请根据已核验结论生成报告。")
    state = HarnessRuntime(runner.store).begin_run(case_id, request.message, run_id="run_report_context_boundary")
    captured: dict[str, Any] = {}

    def fake_call(role: str, role_input: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        assert role == "report_writer"
        captured.update(role_input)
        return {"title": "审核报告", "markdown": "# 审核报告\n"}

    monkeypatch.setattr(runner.roles, "call", fake_call)
    manager_only = "MANAGER_ONLY total=999999.99 verdict=APPROVED"
    result = runner._call_specialist(  # noqa: SLF001
        state,
        request,
        {},
        SupervisorDecision(
            action="delegate_agent",
            target="report_writer",
            input={
                "report_instructions": manager_only,
                "user_request": manager_only,
                "canonical_consumer_packet": {"verdict": manager_only},
            },
        ),
    )

    assert set(captured) == {"canonical_consumer_packet", "user_request"}
    assert captured["user_request"] == request.message
    assert manager_only not in json.dumps(captured, ensure_ascii=False)
    assert result["content_ref"] == "last_role:report_writer.markdown"
    assert "artifact_ref" not in result
    assert result["observation"]["content_ref"] == "last_role:report_writer.markdown"
    assert "artifact_ref" not in result["observation"]

    planner_context = runner.context_assembler.build_planner_context(request, state)
    handoff = planner_context["recent_observations"][-1]
    assert handoff["content_ref"] == "last_role:report_writer.markdown"
    assert "artifact_ref" not in handoff

    normalized = runner._tool_payload_defaults(  # noqa: SLF001
        "write_case_file",
        {"content": manager_only, "content_ref": state.observations[-1]["artifact_ref"]},
        state,
    )
    assert normalized["content_ref"] == "last_role:report_writer.markdown"
    assert "content" not in normalized


def test_report_delivery_summary_uses_persisted_markdown_not_raw_writer_result(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    runner = runtime.runner
    case_id = "case_persisted_report_summary_boundary"
    _seed_ready_case(runner.store, case_id)
    request = AgentTurnRequest(case_id=case_id, message="生成报告，只要 Markdown")
    state = runner.harness.begin_run(case_id, request.message)
    raw_writer = {
        "title": "不可信报告草稿",
        "markdown": "# 草稿\n\n### 摘要结论\n\n该票可付款。\n",
    }
    runner.harness.record_observation(
        state,
        runner.context.record_result(
            state,
            kind="role",
            name="report_writer",
            result=raw_writer,
        ),
    )
    _persist_test_report(
        runner,
        state,
        "# Canonical 报告\n\n### 摘要结论\n\n当前报告仅记录证据状态。\n",
    )

    reply = runner._deterministic_final_after_report(request, state)  # noqa: SLF001

    assert "报告摘要：当前报告仅记录证据状态。" in reply
    assert "可付款" not in reply
    assert "不可信报告草稿" not in reply


def test_report_delivery_summary_keeps_key_amounts_from_persisted_markdown(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    runner = runtime.runner
    case_id = "case_persisted_report_amount_summary"
    _seed_ready_case(runner.store, case_id)
    request = AgentTurnRequest(case_id=case_id, message="生成报告，只要 Markdown")
    state = runner.harness.begin_run(case_id, request.message)
    _persist_test_report(
        runner,
        state,
        (
            "# 审核报告\n\n### 摘要结论\n\n"
            "票面总额 **13,156.92 EUR**，重算总额 13,563.84 EUR，差额 406.92 EUR。\n"
        ),
    )

    reply = runner._deterministic_final_after_report(request, state)  # noqa: SLF001

    assert (
        "报告摘要：票面总额 13,156.92 EUR，重算总额 13,563.84 EUR，差额 406.92 EUR。"
        in reply
    )


@pytest.mark.parametrize(
    ("role", "role_result"),
    [
        ("materials_advisor", {"answer": "请补充采购订单。", "tasks": [], "missing_materials": ["purchase_order"], "next_questions": []}),
        ("report_writer", {"title": "审核报告", "markdown": "# 审核报告\n"}),
    ],
)
def test_nondeterministic_specialist_persists_and_streams_public_progress_around_call(
    tmp_path,
    monkeypatch,
    role: str,
    role_result: dict[str, Any],
) -> None:
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    runner = runtime.runner
    state = HarnessRuntime(runner.store).begin_run(
        f"case_progress_{role}",
        "处理当前案件",
        run_id=f"run_progress_{role}",
    )
    streamed: list[tuple[str, dict[str, Any], str]] = []
    runner._stream_emit = lambda kind, payload, summary="": streamed.append((kind, payload, summary))  # noqa: SLF001

    def fake_call(called_role: str, _role_input: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        assert called_role == role
        assert streamed[-1][0] == "model_thinking"
        assert streamed[-1][1]["status"] == "started"
        persisted = [
            json.loads(line)
            for line in runner.store.resolve_case_path(
                state.case_id,
                f"traces/{state.run_id}/events.jsonl",
            ).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert persisted[-1]["payload"]["status"] == "started"
        return role_result

    monkeypatch.setattr(runner.roles, "call", fake_call)
    result = runner._call_specialist(  # noqa: SLF001
        state,
        AgentTurnRequest(case_id=state.case_id, message="处理当前案件"),
        {},
        SupervisorDecision(action="delegate_agent", target=role, input={}),
    )

    statuses = [
        payload["status"]
        for kind, payload, _summary in streamed
        if kind == "model_thinking" and payload.get("role") == role
    ]
    assert result["status"] == "success"
    assert statuses[0] == "started"
    assert statuses[-1] == "completed"
    persisted_text = runner.store.resolve_case_path(
        state.case_id,
        f"traces/{state.run_id}/events.jsonl",
    ).read_text(encoding="utf-8")
    assert '"status": "completed"' in persisted_text
    assert "reasoning_content" not in persisted_text


def test_nondeterministic_specialist_streams_public_error_without_hidden_reasoning(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    runner = runtime.runner
    state = HarnessRuntime(runner.store).begin_run(
        "case_progress_error",
        "整理补料建议",
        run_id="run_progress_error",
    )
    streamed: list[tuple[str, dict[str, Any], str]] = []
    runner._stream_emit = lambda kind, payload, summary="": streamed.append((kind, payload, summary))  # noqa: SLF001

    def fail_call(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(runner.roles, "call", fail_call)
    result = runner._call_specialist(  # noqa: SLF001
        state,
        AgentTurnRequest(case_id=state.case_id, message="整理补料建议"),
        {},
        SupervisorDecision(action="delegate_agent", target="materials_advisor", input={}),
    )

    progress = [payload for kind, payload, _summary in streamed if kind == "model_thinking"]
    assert result["status"] == "error"
    assert [item["status"] for item in progress] == ["started", "error"]
    assert progress[-1]["error_type"] == "RuntimeError"
    assert "reasoning_content" not in json.dumps(progress, ensure_ascii=False)


def test_report_intent_respects_negative_generation_request() -> None:
    assert not _report_requested_message("请审查这一批材料，判断证据链是否完整，不要生成报告。")
    assert not _report_requested_message("review the evidence without report generation")
    assert not _report_requested_message("review materials and tell me whether the case is ready_for_report")
    assert _report_requested_message("请生成报告并渲染 PDF")
    assert _report_requested_message("final report and render PDF")
    chinese_report_pdf = "\u7ee7\u7eed\u5904\u7406\uff0c\u751f\u6210\u4e2d\u6587\u5ba1\u6838\u62a5\u544a\uff0c\u5e76\u6e32\u67d3 PDF\u3002"
    assert _report_requested_message(chinese_report_pdf)
    assert _policy_report_requested(chinese_report_pdf)


def test_report_intent_recognizes_explicit_report_synonyms_consistently() -> None:
    messages = (
        "我是要出具报告的。",
        "请制作一份审核报告。",
        "我需要一份报告。",
        "I need a report.",
    )

    for message in messages:
        assert _report_requested_message(message)
        assert _policy_report_requested(message)


def test_report_intent_keeps_explicit_negation_authoritative() -> None:
    message = "我是要审核的，但不需要报告。"

    assert not _report_requested_message(message)
    assert not _policy_report_requested(message)


def test_sdk_manager_resume_passes_run_state_as_runner_input(monkeypatch) -> None:
    class FakeRunState:
        def __init__(self) -> None:
            self.approved = False

        def get_interruptions(self) -> list[str]:
            return ["approval"]

        def approve(self, _item: str) -> None:
            self.approved = True

    class FakeResult:
        final_output = "resumed"
        interruptions: list[Any] = []

    run_state = FakeRunState()
    captured: dict[str, Any] = {}

    monkeypatch.setattr("app.runtime.turn_runner.RunState.from_string", lambda *_args, **_kwargs: run_state)

    def fake_run_agent_sync(*args: Any, **kwargs: Any) -> FakeResult:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeResult()

    monkeypatch.setattr("app.runtime.turn_runner.run_agent_sync", fake_run_agent_sync)
    runner = SimpleNamespace(
        context_assembler=SimpleNamespace(build_planner_context=lambda *_args, **_kwargs: {}),
        sdk_tools=lambda **_kwargs: [],
        manager_factory=SimpleNamespace(build=lambda *_args, **_kwargs: "manager"),
        run_config=lambda _state: "run_config",
        record_manager_model_call=lambda *_args, **_kwargs: None,
    )
    state = SimpleNamespace(case_id="case_resume", run_id="run_resume", max_steps=10, step_count=1)

    outcome = SdkManagerRunner().resume(
        runner=runner,
        request=AgentTurnRequest(case_id="case_resume", message="approve"),
        state=state,
        sdk_state="serialized",
        approved=True,
        reason="ok",
    )

    assert outcome.final_output == "resumed"
    assert run_state.approved is True
    assert captured["args"][:2] == ("manager", run_state)
    assert captured["kwargs"]["run_config"] == "run_config"


def test_agent_runtime_uses_manager_policy_loop(tmp_path, monkeypatch) -> None:
    manager = ScriptedManagerRunner(
        [
            {
                "action": "final_answer",
                "final_answer": "你好，我是这个工作台里的发票材料审查助手，可以帮你整理材料、审查证据并生成本地报告/PDF。",
            }
        ]
    )
    runtime = _runtime(tmp_path, monkeypatch, manager)

    response = runtime.run_turn(AgentTurnRequest(case_id="case_live_test", message="你好，你是谁？你能做什么？"))

    assert "发票材料审查助手" in response.reply
    assert response.trace["step_count"] == 1
    assert response.trace["planner_actions"][-1]["action"] == "final_answer"
    assert response.trace["tool_calls"] == []
    assert response.trace["role_calls"] == []
    assert isinstance(manager.payload["capability_cards"]["tools"], list)
    assert manager.payload["capability_cards"]["tools"][0]["risk_level"]
    assert manager.payload["capability_cards"]["skills"]


def test_manager_tool_output_keeps_raw_attachment_in_artifact(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    runner = runtime.runner
    state = HarnessRuntime(runner.store).begin_run("case_manager_tool_output", "review", run_id="run_manager_tool_output")
    source = tmp_path / "invoice.md"
    marker = "DO_NOT_ECHO_RAW_TOOL_CONTENT"
    source.write_text(f"# Invoice\n\n{marker}\n\nTotal: 12.00 CNY", encoding="utf-8")
    request = AgentTurnRequest(
        case_id=state.case_id,
        message="review attachment",
        attachments=[Attachment(name=source.name, path=str(source))],
    )

    result = runner.invoke_manager_tool(
        state=state,
        request=request,
        planner_context={},
        name="read_attachment",
        payload={},
    )

    rendered = json.dumps(result, ensure_ascii=False, default=str)
    assert result["status"] == "success"
    assert result["tool"] == "read_attachment"
    assert "result" not in result
    assert "result_preview" not in result
    assert result["artifact_ref"]
    assert marker not in rendered
    artifact = runner.context.artifacts.read(state.case_id, result["artifact_ref"])
    assert marker in json.dumps(artifact, ensure_ascii=False, default=str)


def test_materials_advisor_preview_keeps_late_policy_blocker(tmp_path, monkeypatch) -> None:
    tasks = [
        {
            "task": f"补充材料 {index}",
            "requirement": f"requirement_{index}",
            "current_status": "missing",
            "why_insufficient": "当前材料不足",
        }
        for index in range(12)
    ]
    tasks.append(
        {
            "task": "配置重复付款检索窗口",
            "requirement": "duplicate_search_window",
            "current_status": "missing",
            "why_insufficient": "企业政策尚未配置该检索期限",
        }
    )
    role_result = {
        "answer": "很长的回答" * 3000,
        "tasks": tasks,
        "missing_materials": ["purchase_order", "duplicate_search_window"],
        "next_questions": ["请确认企业要求的重复付款检索期限。"],
    }
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    runner = runtime.runner
    state = HarnessRuntime(runner.store).begin_run(
        "case_advisor_preview",
        "还缺什么？",
        run_id="run_advisor_preview",
    )
    monkeypatch.setattr(runner.roles, "call", lambda *_args, **_kwargs: role_result)

    result = runner._call_specialist(  # noqa: SLF001
        state,
        AgentTurnRequest(case_id=state.case_id, message="还缺什么？"),
        {},
        SupervisorDecision(action="delegate_agent", target="materials_advisor", input={}),
    )

    preview = result["result_preview"]
    artifact = runner.context.artifacts.read(state.case_id, result["artifact_ref"])
    assert artifact["answer"] == role_result["answer"]
    assert len(preview["answer_summary"]) <= 480
    assert len(preview["tasks"]) == len(tasks)
    assert preview["tasks"][-1] == {
        "requirement": "duplicate_search_window",
        "current_status": "missing",
        "task": "配置重复付款检索窗口",
        "why_insufficient": "企业政策尚未配置该检索期限",
    }
    assert preview["missing_materials"][-1] == "duplicate_search_window"
    assert preview["next_questions"][-1] == "请确认企业要求的重复付款检索期限。"


def test_current_turn_attachment_read_normalizes_manager_path_to_batch(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    runner = runtime.runner
    state = HarnessRuntime(runner.store).begin_run("case_manager_batch_read", "review", run_id="run_manager_batch_read")
    first = tmp_path / "invoice.md"
    second = tmp_path / "po.md"
    first.write_text("# Invoice\n\nINV-BATCH-001", encoding="utf-8")
    second.write_text("# Purchase order\n\nPO-BATCH-001", encoding="utf-8")
    request = AgentTurnRequest(
        case_id=state.case_id,
        message="review attachments",
        attachments=[Attachment(name=first.name, path=str(first)), Attachment(name=second.name, path=str(second))],
    )

    result = runner.invoke_manager_tool(
        state=state,
        request=request,
        planner_context={},
        name="read_attachment",
        payload={"name": first.name, "path": str(first), "max_chars": 12000},
    )

    artifact = runner.context.artifacts.read(state.case_id, result["artifact_ref"])
    assert result["status"] == "success"
    assert len(artifact["attachments"]) == 2
    assert {item["name"] for item in artifact["attachments"]} == {"invoice.md", "po.md"}


def test_policy_blocked_manager_tool_does_not_consume_step_budget(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    runner = runtime.runner
    state = HarnessRuntime(runner.store).begin_run("case_policy_block_budget", "review", run_id="run_policy_block_budget")
    source = tmp_path / "invoice.md"
    source.write_text("# Invoice\n\nTotal: 12.00 CNY", encoding="utf-8")
    request = AgentTurnRequest(
        case_id=state.case_id,
        message="review attachment",
        attachments=[Attachment(name=source.name, path=str(source))],
    )

    result = runner.invoke_manager_tool(
        state=state,
        request=request,
        planner_context={},
        name="evidence_reviewer",
        payload={"mode": "review"},
    )

    assert result["status"] == "blocked"
    assert result["policy_check"]["error_type"] == "attachment_unread"
    assert state.step_count == 0
    assert state.role_calls == []
    assert state.planner_actions == []
    assert any(observation.get("kind") == "policy" and observation.get("name") == "attachment_unread" for observation in state.observations)


def test_evidence_reviewer_timeout_fails_closed_without_fabricated_review(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    runner = runtime.runner
    state = HarnessRuntime(runner.store).begin_run("case_reviewer_timeout_recovery", "review", run_id="run_reviewer_timeout_recovery")
    readme = tmp_path / "README.md"
    invoice = tmp_path / "01_invoice_INV-9201.md"
    duplicate = tmp_path / "05_duplicate_payment_check_INV-9201.md"
    readme.write_text(
        "# Sample case\n\nSuggested desktop test: upload the evidence files. Expected high-level result: review succeeds.",
        encoding="utf-8",
    )
    invoice.write_text(
        "# Invoice INV-9201\n\n- Evidence type: invoice\n- Supplier legal name: Apex Motors Ltd.\n- Invoice total amount: 9900.00 CNY\n",
        encoding="utf-8",
    )
    duplicate.write_text(
        "# Duplicate Payment Check INV-9201\n\n"
        "- Evidence type: duplicate_payment_check\n"
        "- Duplicate invoice found: Yes\n"
        "- Prior payment document: PAY-2026-4431\n"
        "- Clearing document: CLR-2026-4431\n",
        encoding="utf-8",
    )
    request = AgentTurnRequest(
        case_id=state.case_id,
        message="review duplicate payment",
        attachments=[
            Attachment(name=readme.name, path=str(readme)),
            Attachment(name=invoice.name, path=str(invoice)),
            Attachment(name=duplicate.name, path=str(duplicate)),
        ],
    )
    runner.invoke_manager_tool(state=state, request=request, planner_context={}, name="read_attachment", payload={})
    extract_result = {
        "mode": "extract",
        "source_doc_id": "attachment_batch",
        "evidence_type": "invoice",
        "credibility": "high",
        "extracted_fields": {},
        "evidence_cards": [{"title": "extracted invoice"}],
    }
    runner.harness.record_role_call(state, "evidence_reviewer", {"mode": "extract"}, extract_result)
    runner.harness.record_observation(state, runner.context.record_result(state, kind="role", name="evidence_reviewer", result=extract_result))
    runner._update_phase_after_role(state, "evidence_reviewer", extract_result)  # noqa: SLF001
    state.observations = [item for item in state.observations if item.get("kind") != "tool" or item.get("name") != "read_attachment"]

    class APITimeoutError(Exception):
        pass

    def fail_run(_runtime: Any, **_kwargs: Any) -> Any:
        raise APITimeoutError("Request timed out.")

    monkeypatch.setattr("app.runtime.turn_runner.EvidenceCompilerRuntime.run", fail_run)
    runner.llm.calls.append(
        ModelCallRecord(
            role="task_compiler",
            model="test",
            prompt_version="test",
            input_preview="{}",
            output_preview="",
            error="APITimeoutError: Request timed out.",
        )
    )

    review_request = AgentTurnRequest(case_id=state.case_id, message="continue review")
    result = runner.invoke_manager_tool(
        state=state,
        request=review_request,
        planner_context={},
        name="evidence_reviewer",
        payload={"mode": "review", "active_requirement_ids": ["invoice"]},
    )

    assert result["status"] == "error"
    assert result["role"] == "evidence_reviewer"
    assert runner.context.last_evidence_reviewer_result(state, mode="review") == {}


def test_evidence_reviewer_timeout_does_not_promote_pdf_text_to_review(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    runner = runtime.runner
    state = HarnessRuntime(runner.store).begin_run("case_reviewer_pdf_text_recovery", "review", run_id="run_reviewer_pdf_text_recovery")
    batch = {
        "attachments": [
            {
                "status": "success",
                "content_kind": "pdf",
                "extraction_method": "pdf_text",
                "name": "invoice_extract.pdf",
                "attachment_id": "att_pdf_text",
                "body_markdown": "# Invoice INV-PDF-1\n\n- Evidence type: invoice\n- Supplier legal name: PDF Vendor Ltd.\n- Invoice total amount: 319.00 INR\n",
            }
        ]
    }
    ref = runner.context.artifacts.save(state.case_id, state.run_id, "attachment_batch", "read_attachment", batch)
    state.observability["latest_attachment_batch_ref"] = ref

    class APITimeoutError(Exception):
        pass

    def fail_run(_runtime: Any, **_kwargs: Any) -> Any:
        runner.llm.calls.append(
            ModelCallRecord(
                role="task_compiler",
                model="test",
                prompt_version="test",
                input_preview="{}",
                output_preview="",
                error="APITimeoutError: Request timed out.",
            )
        )
        raise APITimeoutError("Request timed out.")

    monkeypatch.setattr("app.runtime.turn_runner.EvidenceCompilerRuntime.run", fail_run)

    review_request = AgentTurnRequest(case_id=state.case_id, message="continue review")
    result = runner.invoke_manager_tool(
        state=state,
        request=review_request,
        planner_context={},
        name="evidence_reviewer",
        payload={"mode": "review", "active_requirement_ids": ["invoice"]},
    )

    assert result["status"] == "error"
    assert runner.context.last_evidence_reviewer_result(state, mode="review") == {}


def test_case_patch_writer_reduces_review_without_llm_call(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    runner = runtime.runner
    state = HarnessRuntime(runner.store).begin_run("case_patch_timeout_recovery", "review", run_id="run_patch_timeout_recovery")
    reviewer_result = {
        "mode": "review",
        "suggested_patch": {
            "requirements": [
                {"id": "invoice", "label": "Invoice", "kind": "document"},
                {"id": "duplicate_payment_screen", "label": "Duplicate payment screen", "status": "conflict", "kind": "risk_check"}
            ],
            "add_evidence": [
                {
                    "id": "ev_dup",
                    "type": "duplicate_payment_check",
                    "credibility": "medium",
                    "summary": "Prior payment PAY-2026-4431 and clearing CLR-2026-4431 require duplicate review.",
                    "source": "attachment",
                    "review_result": {"should_accept": True},
                    "supports": [{"requirement": "duplicate_payment_screen", "support_level": "partial"}],
                    "conflicts": ["duplicate_payment_screen unresolved duplicate payment PAY-2026-4431 / CLR-2026-4431"],
                    "quoted_text": ["Prior payment document: PAY-2026-4431; Clearing document: CLR-2026-4431"],
                }
            ],
            "risk_flags": ["duplicate_payment_hit_unresolved PAY-2026-4431 CLR-2026-4431"],
            "reply_brief": "Potential duplicate payment remains unresolved.",
        },
        "evidence_cards": [{"title": "Duplicate payment check", "doc_type": "duplicate_payment_check"}],
    }
    runner.harness.record_role_call(state, "evidence_reviewer", {}, reviewer_result)
    runner.harness.record_observation(state, runner.context.record_result(state, kind="role", name="evidence_reviewer", result=reviewer_result))
    runner._update_phase_after_role(state, "evidence_reviewer", reviewer_result)  # noqa: SLF001
    state.observability["active_requirement_ids"] = ["invoice", "duplicate_payment_screen"]

    def fail_call(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("deterministic patch reduction must not call the LLM")

    monkeypatch.setattr(runner.roles, "call", fail_call)
    request = AgentTurnRequest(case_id=state.case_id, message="persist review")

    result = runner.invoke_manager_tool(
        state=state,
        request=request,
        planner_context={},
        name="case_patch_writer",
        payload={},
    )
    write_result = runner.invoke_manager_tool(
        state=state,
        request=request,
        planner_context={},
        name="write_case_patch",
        payload={},
    )

    case_state = runner.store.load(state.case_id)
    assert result["status"] == "success"
    assert write_result["status"] == "success"
    assert any(action.get("action") == "write_case_patch" for action in state.planner_actions)
    assert state.role_calls[-1]["capability"]["runtime"] == "deterministic_reducer"
    patch = runner.context.last_role_result(state, name="case_patch_writer")
    assert [item["id"] for item in patch["case_updates"]["requirements"]] == ["invoice", "duplicate_payment_screen"]
    assert not any(call.role == "case_patch_writer" for call in runner.llm.calls)
    assert len(case_state.evidence_items) == 1
    assert "duplicate_payment_screen" not in case_state.conflict_materials
    assert case_state.compiled_proof is None
    assert case_state.review_artifact is None
    assert "PAY-2026-4431" in json.dumps(case_state.model_dump(), ensure_ascii=False)


def test_manager_final_with_boundary_note_is_not_rewritten(tmp_path, monkeypatch) -> None:
    final_answer = "材料已审查。本工具不会执行 ERP 付款或审批流程。"
    manager = ScriptedManagerRunner(
        [
            {
                "action": "final_answer",
                "final_answer": final_answer,
            }
        ]
    )
    runtime = _runtime(tmp_path, monkeypatch, manager)

    response = runtime.run_turn(AgentTurnRequest(case_id="case_final_guard_recovery", message="看看当前材料状态"))

    assert response.reply == final_answer
    assert response.trace["step_count"] == 1
    assert [item["action"] for item in response.trace["planner_actions"]] == ["final_answer"]
    assert not any(item.get("kind") == "guard" for item in response.trace["observations"])
    events = (
        runtime.runner.store.resolve_case_path("case_final_guard_recovery", f"traces/{response.trace['run_id']}/events.jsonl")
    ).read_text(encoding="utf-8")
    assert "final_answer_guard_rewrite" not in events


def test_runtime_policy_guard_rejection_uses_validated_safe_stop_instead_of_raw_text(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    runner = runtime.runner
    request = AgentTurnRequest(case_id="case_runtime_guard_fail_closed", message="总结当前证据状态")
    state = runner.harness.begin_run(request.case_id, request.message)
    rejected = "该票可直接付款。"

    response = runner._finalize_runtime_policy_answer(request, state, rejected)  # noqa: SLF001

    assert rejected not in response.reply
    assert response.reply == (
        "本轮最终回复未通过安全校验，因此未提供业务结论。请查看当前案件状态和运行记录。"
    )
    assert any(
        item.get("kind") == "guard" and item.get("name") == "no_execution_wording"
        for item in response.trace["observations"]
    )
    assert "final_answer_guard_safe_stop" in runner.store.resolve_case_path(
        request.case_id,
        f"traces/{response.trace['run_id']}/events.jsonl",
    ).read_text(encoding="utf-8")


def test_manager_guard_rejection_never_restores_rejected_recovery_text(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    runner = runtime.runner
    request = AgentTurnRequest(case_id="case_manager_guard_fail_closed", message="总结当前证据状态")
    state = runner.harness.begin_run(request.case_id, request.message)
    rejected = "该票可直接付款。"
    monkeypatch.setattr(
        runner,
        "_guard_recovery_final_answer",
        lambda _request, _state: (rejected, "hostile_test_recovery"),
    )

    response = runner._finalize_manager_answer(request, state, rejected)  # noqa: SLF001

    assert response is not None
    assert rejected not in response.reply
    assert response.reply.startswith("本轮最终回复未通过安全校验")


def test_safe_stop_rejection_leaves_empty_state_for_existing_finalize_fallback(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    runner = runtime.runner
    request = AgentTurnRequest(case_id="case_safe_stop_rejected", message="总结当前证据状态")
    state = runner.harness.begin_run(request.case_id, request.message)
    rejected = "该票可直接付款。"
    monkeypatch.setattr(runner.recovery, "handle_final_answer", lambda _state, _decision: None)

    response = runner._finalize_runtime_policy_answer(request, state, rejected)  # noqa: SLF001

    assert state.final_answer == ""
    assert rejected not in response.reply
    assert response.reply == "Agent run stopped without a final answer. Inspect trace for planner observations."


def test_manager_rewrites_meta_planning_preamble_in_same_run(tmp_path, monkeypatch) -> None:
    manager = ScriptedManagerRunner(
        [
            {
                "action": "final_answer",
                "final_answer": "I have all the information needed. Let me provide a comprehensive final response.",
            },
            {
                "action": "final_answer",
                "final_answer": "现有材料不足以支持付款审查；请补充采购订单和收货或服务验收记录。",
            },
        ]
    )
    runtime = _runtime(tmp_path, monkeypatch, manager)

    response = runtime.run_turn(AgentTurnRequest(case_id="case_manager_rewrite", message="请判断材料是否足够。"))

    assert response.reply == "现有材料不足以支持付款审查；请补充采购订单和收货或服务验收记录。"
    assert response.trace["run_id"]
    assert response.trace["step_count"] == 2
    assert any(
        item.get("kind") == "guard" and item.get("name") == "final_answer_internal_retry_instruction"
        for item in response.trace["observations"]
    )
    events = runtime.runner.store.resolve_case_path(
        "case_manager_rewrite", f"traces/{response.trace['run_id']}/events.jsonl"
    ).read_text(encoding="utf-8")
    assert "manager_final_answer_rewrite_requested" in events
    assert "final_answer_guard_rewrite" not in events


def test_guarded_manager_final_delivers_completed_canonical_report_without_rewrite(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    runner = runtime.runner
    case_id = "case_completed_report_guard_recovery"
    _seed_ready_case(runner.store, case_id)
    request = AgentTurnRequest(case_id=case_id, message="生成最终报告并渲染 PDF")
    state = runner.harness.begin_run(case_id, request.message, run_id=f"run_{case_id}")
    state.observations.extend([
        {"kind": "tool", "name": "write_case_file"},
        {"kind": "tool", "name": "render_pdf"},
    ])

    response = runner._finalize_manager_answer(  # noqa: SLF001
        request,
        state,
        "The report is ready. Let me provide the final answer after I verify the invoice again.",
    )

    assert response is not None
    assert "报告已生成" in response.reply
    assert "PDF（主要交付）：reports/final_report_" in response.reply
    assert "Markdown（辅助源文件）：reports/final_report_" in response.reply
    assert "SUPPORTED 1；CONTRADICTED 0；NOT_FOUND 0" in response.reply
    assert "verify the invoice again" not in response.reply
    assert not state.observability.get("_manager_final_rewrite_pending")
    events = runner.store.resolve_case_path(
        case_id,
        f"traces/{state.run_id}/events.jsonl",
    ).read_text(encoding="utf-8")
    assert "canonical_report_delivery" in events
    assert "manager_final_answer_rewrite_requested" not in events


def test_ungrounded_legacy_conflict_does_not_override_manager_or_compiler(tmp_path, monkeypatch) -> None:
    manager = ScriptedManagerRunner(
        [{"action": "final_answer", "final_answer": "重复付款筛查尚未形成可采信证明。"}]
    )
    runtime = _runtime(tmp_path, monkeypatch, manager)
    runtime.runner.store.save(
        CaseState(
            case_id="case_duplicate_history_guard",
            status="collecting_materials",
            requirements=[Requirement(id="duplicate_payment_screen", status="conflict")],
            evidence_items=[
                EvidenceItem(
                    id="ev_history",
                    type="duplicate_payment_check",
                    summary="Prior payment document: PAY-543-HIST; Clearing document: CLR-543-HIST.",
                    review_result={"should_accept": True},
                    supports=[{"requirement": "duplicate_payment_screen", "support_level": "partial"}],
                    conflicts=[
                        {
                            "conflict_type": "duplicate_payment_hit",
                            "requirement": "duplicate_payment_screen",
                            "description": "Prior payment and clearing documents match the invoice.",
                        }
                    ],
                )
            ],
            risk_flags=["historical_payment_document_found", "clearing_document_found"],
        )
    )

    response = runtime.run_turn(AgentTurnRequest(case_id="case_duplicate_history_guard", message="总结重复付款审查结果"))

    assert "status=collecting_materials" not in response.reply
    assert response.case_state.compiled_proof is None
    assert response.case_state.review_artifact is None
    assert response.reply == "重复付款筛查尚未形成可采信证明。"
    events = runtime.runner.store.resolve_case_path(
        "case_duplicate_history_guard", f"traces/{response.trace['run_id']}/events.jsonl"
    ).read_text(encoding="utf-8")
    assert "final_answer_guard_rewrite" not in events


def test_runtime_final_answer_without_canonical_proof_uses_general_case_status(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases")
    state = HarnessRuntime(store).begin_run("case_runtime_final_clean", "review", run_id="run_runtime_final_clean")
    case_state = CaseState(
        case_id=state.case_id,
        status="collecting_materials",
        requirements=[
            Requirement(id="invoice", status="satisfied"),
            Requirement(id="purchase_order", status="satisfied"),
            Requirement(id="goods_receipt_or_service_acceptance", status="satisfied"),
            Requirement(id="vendor_identity", status="satisfied"),
            Requirement(id="duplicate_payment_screen", status="weak"),
        ],
        evidence_items=[
            EvidenceItem(
                id="ev_001",
                type="invoice",
                summary=(
                    "Invoice ID: INV-5001 - Supplier legal name: Orion Parts Co., Ltd, "
                    "Amount: 12800.00 CNY, Date: 2026-04-18, Result: No duplicates found."
                ),
                supports=[{"requirement": "invoice", "support_level": "full"}],
            ),
            EvidenceItem(
                id="ev_005",
                type="duplicate_payment_check",
                summary="Duplicate invoice search result: No duplicate invoice found; Payment history search result: No prior payment found.",
                supports=[{"requirement": "duplicate_payment_screen", "support_level": "partial"}],
            ),
        ],
    )

    answer = _runtime_final_answer(case_state, state)

    assert "下面是当前案卷状态" in answer
    assert "status=collecting_materials" in answer
    assert "weak=duplicate_payment_screen" in answer
    assert "Orion Parts Co., Ltd." not in answer
    assert "重复付款检查" not in answer


def test_runtime_final_answer_without_canonical_proof_does_not_expand_domain_risk(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases")
    state = HarnessRuntime(store).begin_run("case_bank_change", "review", run_id="run_bank_change")
    case_state = CaseState(
        case_id=state.case_id,
        status="collecting_materials",
        requirements=[Requirement(id="vendor_identity", status="conflict")],
        risk_flags=["bank_change_request_without_vendor_master_approval"],
    )

    answer = _runtime_final_answer(case_state, state)

    assert "bank_change_request_without_vendor_master_approval" in answer
    assert "供应商名称等身份字段已匹配" not in answer
    assert "银行账户变更缺少供应商主数据审批依据" not in answer


def test_manager_cannot_finalize_attachment_review_before_case_patch(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    request = AgentTurnRequest(case_id="case_unpatched_attachment", message="review attachment")
    runtime.runner.store.save(CaseState(case_id=request.case_id))
    state = runtime.runner.harness.begin_run(request.case_id, request.message)
    runtime.runner.harness.record_observation(state, {"kind": "tool", "name": "read_attachment"})

    response = runtime.runner._finalize_manager_answer(request, state, "All evidence matches and is complete.")

    assert "All evidence matches" not in response.reply
    assert "status=new" in response.reply


def test_manager_stale_explicit_requirement_status_rewrites_from_canonical_case(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    request = AgentTurnRequest(case_id="case_stale_status", message="总结当前审核结果")
    _seed_ready_case(runtime.runner.store, request.case_id)
    state = runtime.runner.harness.begin_run(request.case_id, request.message)

    response = runtime.runner._finalize_manager_answer(
        request,
        state,
        "| invoice | weak |",
    )

    assert "invoice=SUPPORTED" in response.reply
    assert "satisfied=invoice" in response.reply
    assert "| invoice | weak |" not in response.reply
    events = runtime.runner.store.resolve_case_path(
        request.case_id,
        f"traces/{response.trace['run_id']}/events.jsonl",
    ).read_text(encoding="utf-8")
    assert "final_answer_guard_rewrite" in events


def test_attachment_review_waits_for_manager_scope_when_none_was_selected(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    request = AgentTurnRequest(case_id="case_default_scope", message="review payment materials")
    state = runtime.runner.harness.begin_run(request.case_id, request.message)
    runtime.runner.harness.record_observation(state, {"kind": "tool", "name": "read_attachment"})

    assert runtime.runner._deterministic_policy_continuation(request, state) is None


def test_existing_partial_report_continues_to_file_write_without_ready_status(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    request = AgentTurnRequest(case_id="case_partial_report", message="生成最终报告并渲染 PDF")
    runtime.runner.store.save(CaseState(case_id=request.case_id, status="collecting_materials"))
    state = runtime.runner.harness.begin_run(request.case_id, request.message)

    assert runtime.runner._deterministic_policy_continuation(request, state) is None

    report = {"title": "部分审核报告", "markdown": "# 部分审核报告\n\n存在待补证据。\n"}
    runtime.runner.harness.record_observation(
        state,
        runtime.runner.context.record_result(state, kind="role", name="report_writer", result=report),
    )

    assert runtime.runner._deterministic_policy_continuation(request, state) == ("write_case_file", {})


@pytest.mark.parametrize(
    ("compiler_status", "case_status", "requirement_status"),
    [
        ("SUPPORTED", "ready_for_report", "satisfied"),
        ("CONTRADICTED", "ready_for_report", "conflict"),
        ("NOT_FOUND", "collecting_materials", "missing"),
    ],
)
def test_runtime_final_answer_renders_any_compiler_decision_generically(
    tmp_path,
    compiler_status: str,
    case_status: str,
    requirement_status: str,
) -> None:
    store = CaseStore(tmp_path)
    state = HarnessRuntime(store).begin_run("case_compiler_decision", "审核", run_id="run_compiler_decision")
    proof = _runtime_proof(
        "policy_alignment",
        compiler_status,
        missing_fact="需要权威来源确认政策前提" if compiler_status == "NOT_FOUND" else "",
    )
    case_state = CaseState(
        case_id=state.case_id,
        status=case_status,
        requirements=[Requirement(id="policy_alignment", status=requirement_status)],
        evidence_items=[EvidenceItem(id="ev_proof", type="unknown", summary="PAY-771 and CLR-771 are irrelevant lexical noise.")],
        risk_flags=["duplicate_payment_risk lexical noise"],
        compiled_proof=proof,
    )

    answer = _runtime_final_answer(case_state, state)

    assert f"policy_alignment={compiler_status}" in answer
    assert "PAY-771" not in answer
    assert "CLR-771" not in answer
    assert "重复付款" not in answer
    if compiler_status == "NOT_FOUND":
        assert "obligation:policy_alignment:check.policy_alignment" in answer
        assert "需要权威来源确认政策前提" in answer
    else:
        assert "尚未完成的核查：无" in answer


def test_runtime_final_answer_preserves_not_found_with_weak_projection(tmp_path) -> None:
    store = CaseStore(tmp_path)
    state = HarnessRuntime(store).begin_run("case_partial_proof", "审核", run_id="run_partial_proof")
    case_state = CaseState(
        case_id=state.case_id,
        status="collecting_materials",
        requirements=[Requirement(id="policy_alignment", status="weak", evidence_ids=["ev_partial"])],
        evidence_items=[EvidenceItem(id="ev_partial", type="unknown", summary="Partial source only.")],
        compiled_proof=_runtime_proof(
            "policy_alignment",
            "NOT_FOUND",
            missing_fact="仍缺少完成核查所需的权威来源",
        ),
    )

    answer = _runtime_final_answer(case_state, state)

    # DecisionProof and Requirement projection are intentionally different layers:
    # a NOT_FOUND proof with a partial source projects to weak, never SUPPORTED.
    assert "policy_alignment=NOT_FOUND" in answer
    assert "weak=policy_alignment" in answer
    assert "仍有 NOT_FOUND 原子检查" in answer
    assert "policy_alignment=SUPPORTED" not in answer


def test_runtime_final_answer_keeps_uncompiled_requirement_blockers(tmp_path) -> None:
    store = CaseStore(tmp_path)
    state = HarnessRuntime(store).begin_run("case_mixed_requirements", "审核", run_id="run_mixed_requirements")
    case_state = CaseState(
        case_id=state.case_id,
        status="collecting_materials",
        requirements=[
            Requirement(id="policy_alignment", status="satisfied"),
            Requirement(id="uncompiled_supporting_record", status="missing"),
        ],
        risk_flags=["manual_follow_up_required"],
        next_questions=["请补充非编译支持记录。"],
        compiled_proof=_runtime_proof("policy_alignment", "SUPPORTED"),
    )

    answer = _runtime_final_answer(case_state, state)

    assert "policy_alignment=SUPPORTED" in answer
    assert "missing=uncompiled_supporting_record" in answer
    assert "manual_follow_up_required" in answer
    assert "请补充非编译支持记录" in answer
    assert "可进入报告" not in answer


def test_evidence_reviewer_mode_mismatch_becomes_policy_feedback(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    runner = runtime.runner
    state = HarnessRuntime(runner.store).begin_run("case_mode_mismatch", "review", run_id="run_mode_mismatch")

    mismatch = runner._record_reviewer_mode_mismatch(
        state,
        "evidence_reviewer",
        {"mode": "extract"},
        {"mode": "review", "reason": "model ignored requested mode"},
        {"prompt_version": "test"},
    )

    assert mismatch is True
    assert state.observations[-1]["kind"] == "policy"
    assert state.observations[-1]["name"] == "role_mode_mismatch"
    assert state.observations[-1]["next_action_hint"] == "delegate_agent:evidence_reviewer_extract"
    assert state.role_calls[-1]["error"]


def test_evidence_reviewer_extract_shape_repairs_wrong_mode(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    runner = runtime.runner
    state = HarnessRuntime(runner.store).begin_run("case_mode_repair", "review", run_id="run_mode_repair")
    result = {
        "mode": "review",
        "extracted_fields": {"invoice_number": {"value": "INV-1"}},
        "extraction_result": {"source_docs": [{"doc_id": "att_1"}]},
        "suggested_patch": {},
    }

    mismatch = runner._record_reviewer_mode_mismatch(
        state,
        "evidence_reviewer",
        {"mode": "extract"},
        result,
        {"prompt_version": "test"},
    )

    assert mismatch is False
    assert result["mode"] == "extract"


def test_agent_runtime_interrupts_before_approved_tool_execution(tmp_path, monkeypatch) -> None:
    manager = ScriptedManagerRunner(
        [
            {"action": "call_tool", "target": "list_case_files", "input": {}, "reason": "inspect files"},
            {"action": "final_answer", "final_answer": "已列出本地 case 文件。"},
        ]
    )
    runtime = _runtime(tmp_path, monkeypatch, manager)
    runner = runtime.runner
    runner.tools._specs["list_case_files"] = replace(runner.tools.get("list_case_files"), approval_mode="always")  # noqa: SLF001

    response = runtime.run_turn(AgentTurnRequest(case_id="case_approval_ok", message="列一下文件"))

    assert response.trace["status"] == "waiting_approval"
    assert response.trace["tool_calls"] == []
    assert response.trace["interrupts"][0]["tool"] == "list_case_files"
    assert response.trace["interrupts"][0]["risk_level"] == "read"
    assert response.trace["interrupts"][0]["input_sha256"]
    assert "input" not in response.trace["interrupts"][0]

    resumed = runtime.resume_approval("case_approval_ok", response.trace["run_id"], approved=True, reason="ok")

    assert resumed.trace["tool_calls"][0]["tool"] == "list_case_files"
    assert resumed.trace["tool_calls"][0]["capability"]["approval_mode"] == "always"
    assert resumed.reply == "已列出本地 case 文件。"


def test_approval_resume_continues_after_events_flushed_past_checkpoint(tmp_path, monkeypatch) -> None:
    manager = ScriptedManagerRunner(
        [
            {"action": "call_tool", "target": "list_case_files", "input": {}, "reason": "inspect files"},
            {"action": "final_answer", "final_answer": "done"},
        ]
    )
    runtime = _runtime(tmp_path, monkeypatch, manager)
    runner = runtime.runner
    runner.tools._specs["list_case_files"] = replace(runner.tools.get("list_case_files"), approval_mode="always")  # noqa: SLF001
    waiting = runtime.run_turn(AgentTurnRequest(case_id="case_seq_resume", message="list files"))
    stale_state, _request, _sdk_state, _interruptions = runner.checkpoints.load(
        "case_seq_resume",
        waiting.trace["run_id"],
    )
    runner.harness.append_debug_event(
        stale_state,
        kind="provider_call",
        name="planner",
        payload={"call_number": 7, "status": "OK"},
        summary="late provider flush",
    )

    final = runtime.resume_approval(
        "case_seq_resume",
        waiting.trace["run_id"],
        approved=True,
        reason="ok",
    )
    rows = [
        json.loads(line)
        for line in runner.store.resolve_case_path(
            "case_seq_resume",
            f"traces/{waiting.trace['run_id']}/events.jsonl",
        ).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert final.reply == "done"
    assert sum(1 for row in rows if row["kind"] == "provider_call") == 1
    assert len({row["event_id"] for row in rows}) == len(rows)
    assert [row["run_seq"] for row in rows] == list(range(1, len(rows) + 1))


def test_agent_runtime_rejected_approval_does_not_execute_tool(tmp_path, monkeypatch) -> None:
    manager = ScriptedManagerRunner(
        [
            {"action": "call_tool", "target": "list_case_files", "input": {}, "reason": "inspect files"},
            {"action": "final_answer", "final_answer": "已取消查看文件，未执行该工具。"},
        ]
    )
    runtime = _runtime(tmp_path, monkeypatch, manager)
    runtime.runner.tools._specs["list_case_files"] = replace(runtime.runner.tools.get("list_case_files"), approval_mode="always")  # noqa: SLF001

    response = runtime.run_turn(AgentTurnRequest(case_id="case_approval_no", message="列一下文件"))
    resumed = runtime.resume_approval("case_approval_no", response.trace["run_id"], approved=False, reason="no")

    assert resumed.trace["tool_calls"] == []
    assert resumed.reply == "已取消查看文件，未执行该工具。"
    assert any(observation["kind"] == "approval" and observation["name"] == "rejected" for observation in resumed.trace["observations"])


@pytest.mark.parametrize("streamed", [False, True])
def test_sdk_report_approval_uses_deterministic_write_render_and_final(
    tmp_path,
    monkeypatch,
    streamed: bool,
) -> None:
    manager = ReportSdkStateMustNotResumeManager()
    runtime = _runtime(tmp_path, monkeypatch, manager)
    case_id = f"case_sdk_report_{'stream' if streamed else 'sync'}"
    _seed_ready_case(runtime.runner.store, case_id)
    monkeypatch.setattr(runtime.runner.roles, "call", _fake_report_writer)
    response = _checkpoint_sdk_report_write_approval(runtime, case_id)

    render_waiting = _resume_test_approval(runtime, case_id, response, approved=True, streamed=streamed)
    assert render_waiting.trace["status"] == "waiting_approval"
    assert render_waiting.trace["interrupts"][0]["tool"] == "render_pdf"
    assert list((tmp_path / "cases" / case_id / "reports").glob("*.md"))
    assert not list((tmp_path / "cases" / case_id / "reports").glob("*.pdf"))

    final = _resume_test_approval(runtime, case_id, render_waiting, approved=True, streamed=streamed)

    assert manager.run_calls == 0
    assert manager.resume_calls == 0
    assert final.trace["phase"] == "finalized"
    assert [call["tool"] for call in final.trace["tool_calls"] if call["tool"] in {"write_case_file", "render_pdf"}] == [
        "write_case_file",
        "render_pdf",
    ]
    assert "PDF（主要交付）：reports/final_report_" in final.reply
    assert "Markdown（辅助源文件）：reports/final_report_" in final.reply
    assert "SUPPORTED 1；CONTRADICTED 0；NOT_FOUND 0" in final.reply
    assert list((tmp_path / "cases" / case_id / "reports").glob("*.pdf"))
    assert sum(
        1
        for observation in final.trace["observations"]
        if observation["kind"] == "approval" and observation["name"] == "approved"
    ) == 2


@pytest.mark.parametrize("streamed", [False, True])
def test_report_writer_success_at_manager_max_turns_starts_write_approval_and_completes_delivery(
    tmp_path,
    monkeypatch,
    streamed: bool,
) -> None:
    manager = ReportWriterThenMaxTurnsManager()
    runtime = _runtime(tmp_path, monkeypatch, manager)
    case_id = f"case_report_max_turns_{'stream' if streamed else 'sync'}"
    _seed_ready_case(runtime.runner.store, case_id)
    monkeypatch.setattr(runtime.runner.roles, "call", _fake_report_writer)

    waiting = _run_report_manager_until_max_turns(runtime, case_id, streamed=streamed)

    assert manager.run_calls == 1
    assert waiting.trace["status"] == "waiting_approval"
    assert waiting.trace["interrupts"][0]["tool"] == "write_case_file"
    assert ("runtime_recovery", "report_write_after_manager_max_turns") in _runtime_event_pairs(
        runtime,
        case_id,
        waiting.trace["run_id"],
    )

    render_waiting = _resume_test_approval(runtime, case_id, waiting, approved=True, streamed=streamed)
    assert render_waiting.trace["status"] == "waiting_approval"
    assert render_waiting.trace["interrupts"][0]["tool"] == "render_pdf"

    final = _resume_test_approval(runtime, case_id, render_waiting, approved=True, streamed=streamed)

    assert final.trace["phase"] == "finalized"
    assert "报告已生成" in final.reply
    assert "PDF（主要交付）" in final.reply
    assert "证据结论" in final.reply
    assert list((tmp_path / "cases" / case_id / "reports").glob("*.md"))
    assert list((tmp_path / "cases" / case_id / "reports").glob("*.pdf"))


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("failure_boundary", ["no_writer", "different_continuation"])
def test_manager_max_turns_outside_exact_report_write_boundary_still_fails(
    tmp_path,
    monkeypatch,
    streamed: bool,
    failure_boundary: str,
) -> None:
    manager: ScriptedManagerRunner
    if failure_boundary == "no_writer":
        manager = MaxTurnsWithoutWriterManager()
    else:
        manager = ReportWriterThenMaxTurnsManager()
    runtime = _runtime(tmp_path, monkeypatch, manager)
    case_id = f"case_report_max_turns_{failure_boundary}_{'stream' if streamed else 'sync'}"
    _seed_ready_case(runtime.runner.store, case_id)
    monkeypatch.setattr(runtime.runner.roles, "call", _fake_report_writer)

    response = _run_report_manager_until_max_turns(
        runtime,
        case_id,
        streamed=streamed,
        prior_write_observation=failure_boundary == "different_continuation",
    )

    assert response.trace["phase"] == "finalized"
    assert response.trace.get("interrupts") in (None, [])
    assert "case manager 调用失败" in response.reply
    assert ("runtime_recovery", "report_write_after_manager_max_turns") not in _runtime_event_pairs(
        runtime,
        case_id,
        response.trace["run_id"],
    )


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("rejected_tool", ["write_case_file", "render_pdf"])
def test_sdk_report_approval_rejection_never_executes_rejected_tool(
    tmp_path,
    monkeypatch,
    streamed: bool,
    rejected_tool: str,
) -> None:
    manager = ReportSdkStateMustNotResumeManager()
    runtime = _runtime(tmp_path, monkeypatch, manager)
    case_id = f"case_sdk_reject_{rejected_tool}_{'stream' if streamed else 'sync'}"
    _seed_ready_case(runtime.runner.store, case_id)
    monkeypatch.setattr(runtime.runner.roles, "call", _fake_report_writer)
    waiting = _checkpoint_sdk_report_write_approval(runtime, case_id)

    if rejected_tool == "render_pdf":
        waiting = _resume_test_approval(runtime, case_id, waiting, approved=True, streamed=streamed)
        assert waiting.trace["interrupts"][0]["tool"] == "render_pdf"
    final = _resume_test_approval(runtime, case_id, waiting, approved=False, streamed=streamed)

    report_dir = tmp_path / "cases" / case_id / "reports"
    assert manager.run_calls == 0
    assert manager.resume_calls == 0
    assert final.trace["phase"] == "finalized"
    assert f"已取消执行 {rejected_tool}" in final.reply
    assert rejected_tool not in {call["tool"] for call in final.trace["tool_calls"]}
    assert not list(report_dir.glob("*.pdf"))
    if rejected_tool == "write_case_file":
        assert not list(report_dir.glob("*.md"))
    else:
        assert list(report_dir.glob("*.md"))
    assert any(
        observation["kind"] == "approval" and observation["name"] == "rejected"
        for observation in final.trace["observations"]
    )


def test_report_request_runs_file_and_pdf_approval_pipeline(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    _seed_ready_case(runtime.runner.store, "case_report_approval")
    monkeypatch.setattr(runtime.runner.roles, "call", _fake_report_writer)

    response = runtime.run_turn(AgentTurnRequest(case_id="case_report_approval", message="final report and render PDF"))

    assert response.trace["status"] == "waiting_approval"
    assert response.trace["interrupts"][0]["tool"] == "write_case_file"
    assert response.trace["role_calls"][0]["role"] == "report_writer"
    assert not list((tmp_path / "cases" / "case_report_approval" / "reports").glob("*.md"))
    waiting_trace = json.loads((tmp_path / "cases" / "case_report_approval" / "traces" / f"{response.trace['run_id']}.json").read_text(encoding="utf-8"))
    assert waiting_trace["interrupts"][0]["tool"] == "write_case_file"

    resumed = runtime.resume_approval("case_report_approval", response.trace["run_id"], approved=True, reason="ok")

    assert resumed.trace["status"] == "waiting_approval"
    assert resumed.trace["interrupts"][0]["tool"] == "render_pdf"
    assert list((tmp_path / "cases" / "case_report_approval" / "reports").glob("*.md"))
    assert not list((tmp_path / "cases" / "case_report_approval" / "reports").glob("*.pdf"))
    checkpoint = tmp_path / "cases" / "case_report_approval" / "traces" / response.trace["run_id"] / "runtime_state.json"
    checkpoint_data = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert checkpoint_data["interruptions"][0]["tool"] == "render_pdf"

    final = runtime.resume_approval("case_report_approval", resumed.trace["run_id"], approved=True, reason="ok")

    assert final.trace["phase"] == "finalized"
    assert "PDF（主要交付）：reports/final_report_" in final.reply
    assert "Markdown（辅助源文件）：reports/final_report_" in final.reply
    assert "SUPPORTED 1；CONTRADICTED 0；NOT_FOUND 0" in final.reply
    assert "不等于业务批准或拒绝" in final.reply
    assert {call["tool"] for call in final.trace["tool_calls"]} >= {"write_case_file", "render_pdf"}
    write_call = next(call for call in final.trace["tool_calls"] if call["tool"] == "write_case_file")
    assert write_call["input"]["content_ref"] == "last_role:report_writer.markdown"
    assert "content" not in write_call["input"]
    assert list((tmp_path / "cases" / "case_report_approval" / "reports").glob("*.pdf"))
    assert any(event["kind"] == "approval" and event["name"] == "approved" for event in final.trace["observations"])
    final_trace = json.loads((tmp_path / "cases" / "case_report_approval" / "traces" / f"{response.trace['run_id']}.json").read_text(encoding="utf-8"))
    assert final_trace["interrupts"] == []
    assert not checkpoint.exists()
    event_rows = [
        json.loads(line)
        for line in (tmp_path / "cases" / "case_report_approval" / "traces" / response.trace["run_id"] / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    run_seqs = [int(item["run_seq"]) for item in event_rows]
    event_ids = [str(item["event_id"]) for item in event_rows]
    assert run_seqs == list(range(1, len(run_seqs) + 1))
    assert len(event_ids) == len(set(event_ids))
    assert [item["name"] for item in event_rows if item["kind"] == "approval_interrupt"] == [
        "write_case_file",
        "render_pdf",
    ]


@pytest.mark.parametrize(
    ("status", "missing_fact", "expected"),
    [
        ("CONTRADICTED", "", "首个已证实冲突：invoice"),
        ("NOT_FOUND", "a supplier master record", "首个未决项：invoice（a supplier master record）"),
    ],
)
def test_report_completion_summarizes_first_canonical_exception(
    tmp_path,
    monkeypatch,
    status: str,
    missing_fact: str,
    expected: str,
) -> None:
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    state = HarnessRuntime(runtime.runner.store).begin_run(
        f"case-report-summary-{status.lower()}",
        "生成报告，只要 Markdown",
        run_id=f"run-report-summary-{status.lower()}",
    )
    state.observations.append({"kind": "tool", "name": "write_case_file"})
    case_state = CaseState(
        case_id=state.case_id,
        compiled_proof=_runtime_proof("invoice", status, missing_fact=missing_fact),
    )
    monkeypatch.setattr(runtime.runner.store, "load", lambda _case_id: case_state)

    reply = runtime.runner._deterministic_final_after_report(  # noqa: SLF001
        AgentTurnRequest(case_id=state.case_id, message="生成报告，只要 Markdown"),
        state,
    )

    assert expected in reply
    assert f"{status} 1" in reply
    assert "不等于业务批准或拒绝" in reply


def test_report_completion_wins_over_step_limit_after_pdf_render(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("INVOICE_AGENT_MAX_STEPS", "5")
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    _seed_ready_case(runtime.runner.store, "case_report_step_limit")
    monkeypatch.setattr(runtime.runner.roles, "call", _fake_report_writer)

    response = runtime.run_turn(AgentTurnRequest(case_id="case_report_step_limit", message="final report and render PDF"))
    assert response.trace["status"] == "waiting_approval"
    response = runtime.resume_approval("case_report_step_limit", response.trace["run_id"], approved=True, reason="ok")
    assert response.trace["status"] == "waiting_approval"

    final = runtime.resume_approval("case_report_step_limit", response.trace["run_id"], approved=True, reason="ok")

    assert final.trace["phase"] == "finalized"
    assert final.trace["step_count"] >= final.trace["max_steps"]
    assert "报告已生成" in final.reply
    assert "当前运行已到本轮步数上限" not in final.reply
    assert {call["tool"] for call in final.trace["tool_calls"]} >= {"write_case_file", "render_pdf"}
    assert list((tmp_path / "cases" / "case_report_step_limit" / "reports").glob("*.pdf"))


def test_report_request_rejected_approval_does_not_write_file(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    _seed_ready_case(runtime.runner.store, "case_report_rejected")
    monkeypatch.setattr(runtime.runner.roles, "call", _fake_report_writer)

    response = runtime.run_turn(AgentTurnRequest(case_id="case_report_rejected", message="final report and render PDF"))
    final = runtime.resume_approval("case_report_rejected", response.trace["run_id"], approved=False, reason="no")

    assert final.trace["phase"] == "finalized"
    assert "已取消执行 write_case_file" in final.reply
    assert not list((tmp_path / "cases" / "case_report_rejected" / "reports").glob("*.md"))
    assert "write_case_file" not in {call["tool"] for call in final.trace["tool_calls"]}
    assert any(event["kind"] == "approval" and event["name"] == "rejected" for event in final.trace["observations"])
