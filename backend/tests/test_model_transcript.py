from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from agents.usage import Usage
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails
from pydantic import BaseModel

import app.compiler_runtime.transcript as transcript_module
from app.agents.capabilities import ROLE_CAPABILITIES
from app.compiler_runtime.runtime import PROMPT_VERSIONS
from app.compiler_runtime.transcript import ModelTranscriptHooks
from app.config import Settings
from app.harness import HarnessRuntime
from app.llm import LlmClient
from app.runtime.context_partition import usage_from_result
from app.runtime.turn_runner import MANAGER_PROMPT_VERSION, TurnRunner
from app.state.case_store import CaseStore


def _run(awaitable) -> None:
    asyncio.run(awaitable)


def test_usage_from_result_serializes_sdk_usage_dataclass() -> None:
    result = SimpleNamespace(
        raw_responses=[
            SimpleNamespace(
                usage=Usage(
                    requests=1,
                    input_tokens=12,
                    input_tokens_details=InputTokensDetails(cached_tokens=4),
                    output_tokens=3,
                    output_tokens_details=OutputTokensDetails(reasoning_tokens=2),
                    total_tokens=15,
                )
            )
        ]
    )

    assert usage_from_result(result) == {
        "prompt_tokens": 12,
        "completion_tokens": 3,
        "total_tokens": 15,
        "cached_tokens": 4,
        "reasoning_tokens": 2,
    }


def test_hooks_write_one_redacted_visible_provider_call_with_tools(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases")
    harness = HarnessRuntime(store)
    state = harness.begin_run("case_transcript", "test", run_id="run_transcript")
    hooks = ModelTranscriptHooks(
        harness,
        state,
        prompt_version="executor_v1",
        secret_values=["private-token-value"],
    )
    agent = SimpleNamespace(name="executor", model="deepseek-v4-flash")
    context = SimpleNamespace(tool_call_id="tool_1", tool_arguments='{"source_id":"invoice"}')
    tool = SimpleNamespace(name="read_source")
    response = SimpleNamespace(
        output=[
            {"type": "reasoning", "summary": "hidden chain of thought"},
            {
                "type": "function_call",
                "name": "read_source",
                "arguments": '{"source_id":"invoice"}',
            },
            {"type": "message", "content": [{"type": "output_text", "text": "visible answer"}]},
        ],
        usage=Usage(requests=1, input_tokens=20, output_tokens=7, total_tokens=27),
        request_id="req_123",
        response_id="resp_123",
    )
    fake_sk_secret = "sk-" + "1234567890abcdef"

    _run(
        hooks.on_llm_start(
            SimpleNamespace(),
            agent,
            (
                "Use api_key=private-token-value and Authorization: Bearer secret-bearer "
                'with {"access_token": "opaque-access-secret"}'
            ),
            [
                {"role": "user", "content": f"invoice; {fake_sk_secret}"},
                {"type": "reasoning", "summary": "hidden previous reasoning"},
            ],
        )
    )
    _run(hooks.on_llm_end(SimpleNamespace(), agent, response))
    _run(hooks.on_tool_start(context, agent, tool))
    _run(hooks.on_tool_end(context, agent, tool, "invoice total is GBP 100"))
    _run(hooks.on_agent_end(SimpleNamespace(), agent, "done"))

    transcript = store.resolve_case_path(
        "case_transcript", "traces/run_transcript/deepseek_calls.txt"
    ).read_text(encoding="utf-8")
    assert "调用 001 | executor | deepseek-v4-flash | OK" in transcript
    assert "visible answer" in transcript
    assert "invoice total is GBP 100" in transcript
    assert '"prompt_tokens": 20' in transcript
    assert "private-token-value" not in transcript
    assert "secret-bearer" not in transcript
    assert "opaque-access-secret" not in transcript
    assert fake_sk_secret not in transcript
    assert "hidden chain of thought" not in transcript
    assert "hidden previous reasoning" not in transcript

    events_path = store.resolve_case_path(
        "case_transcript", "traces/run_transcript/events.jsonl"
    )
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert len(events) == 1
    assert events[0]["kind"] == "provider_call"
    assert events[0]["payload"]["usage"]["total_tokens"] == 27
    assert events[0]["payload"]["tools"][0]["result"] == "invoice total is GBP 100"


def test_hooks_record_every_call_and_provider_error(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases")
    harness = HarnessRuntime(store)
    state = harness.begin_run("case_calls", "test", run_id="run_calls")
    hooks = ModelTranscriptHooks(harness, state)
    agent = SimpleNamespace(name="task_compiler", model="deepseek-v4-flash")
    response = SimpleNamespace(
        output=[{"type": "message", "content": [{"type": "output_text", "text": "plan"}]}],
        usage=Usage(requests=1, input_tokens=5, output_tokens=2, total_tokens=7),
        request_id="req_ok",
        response_id="resp_ok",
    )

    _run(hooks.on_llm_start(SimpleNamespace(), agent, "compile", [{"role": "user", "content": "case"}]))
    _run(hooks.on_llm_end(SimpleNamespace(), agent, response))
    _run(hooks.on_llm_start(SimpleNamespace(), agent, "retry", [{"role": "user", "content": "case"}]))
    hooks.record_error(TimeoutError("provider timed out"))

    transcript = store.resolve_case_path("case_calls", "traces/run_calls/deepseek_calls.txt").read_text(
        encoding="utf-8"
    )
    assert transcript.count("【SYSTEM / 问了什么】") == 2
    assert "调用 001 | task_compiler | deepseek-v4-flash | OK" in transcript
    assert "调用 002 | task_compiler | deepseek-v4-flash | ERROR" in transcript
    assert "TimeoutError: provider timed out" in transcript

    events_path = store.resolve_case_path("case_calls", "traces/run_calls/events.jsonl")
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert [event["payload"]["call_number"] for event in events] == [1, 2]


def test_hooks_use_each_agent_prompt_version(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases")
    harness = HarnessRuntime(store)
    state = harness.begin_run("case_versions", "test", run_id="run_versions")
    hooks = ModelTranscriptHooks(
        harness,
        state,
        prompt_version="fallback_v1",
        prompt_versions={
            "task_compiler": "plan_v1",
            "executor": "executor_v2",
            "fine_verifier": "verifier_v3",
            "case_manager": "manager_v4",
            "report_writer": "writer_v5",
        },
    )

    for name in ("task_compiler", "executor", "fine_verifier", "case_manager", "report_writer"):
        agent = SimpleNamespace(name=name, model="deepseek-v4-flash")
        _run(hooks.on_llm_start(SimpleNamespace(), agent, name, []))
        hooks.record_error(RuntimeError("test"))

    events_path = store.resolve_case_path("case_versions", "traces/run_versions/events.jsonl")
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert [event["payload"]["prompt_version"] for event in events] == [
        "plan_v1",
        "executor_v2",
        "verifier_v3",
        "manager_v4",
        "writer_v5",
    ]


def test_turn_runner_wires_current_prompt_versions(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases")
    runner = TurnRunner(
        store=store,
        llm=LlmClient(Settings(llm_api_key="test", llm_model="deepseek-v4-flash")),
    )
    state = runner.harness.begin_run("case_versions", "test", run_id="run_versions")

    assert runner.transcript_hooks(state).prompt_versions == {
        "case_manager": MANAGER_PROMPT_VERSION,
        "planner": MANAGER_PROMPT_VERSION,
        "task_compiler": PROMPT_VERSIONS["task_compiler"],
        "executor": PROMPT_VERSIONS["executor"],
        "fine_verifier": PROMPT_VERSIONS["verifier"],
        "materials_advisor": ROLE_CAPABILITIES["materials_advisor"].prompt_version,
        "report_writer": ROLE_CAPABILITIES["report_writer"].prompt_version,
    }


def test_transcript_write_failure_is_only_a_trace_warning(tmp_path, monkeypatch) -> None:
    store = CaseStore(tmp_path / "cases")
    harness = HarnessRuntime(store)
    state = harness.begin_run("case_write_failure", "test", run_id="run_write_failure")
    hooks = ModelTranscriptHooks(harness, state)
    agent = SimpleNamespace(name="verifier", model="deepseek-v4-flash")
    response = SimpleNamespace(
        output=[{"type": "message", "content": [{"type": "output_text", "text": "supported"}]}],
        usage=Usage(requests=1, input_tokens=3, output_tokens=1, total_tokens=4),
        request_id="req_warning",
        response_id="resp_warning",
    )
    monkeypatch.setattr(
        transcript_module,
        "append_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk unavailable")),
    )

    _run(hooks.on_llm_start(SimpleNamespace(), agent, "verify", []))
    _run(hooks.on_llm_end(SimpleNamespace(), agent, response))
    _run(hooks.on_agent_end(SimpleNamespace(), agent, "supported"))

    events_path = store.resolve_case_path(
        "case_write_failure", "traces/run_write_failure/events.jsonl"
    )
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert [event["kind"] for event in events] == ["provider_call", "trace_warning"]
    assert events[-1]["name"] == "model_transcript_write_failed"


def test_nested_agent_keeps_outer_tool_result_and_start_order(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases")
    harness = HarnessRuntime(store)
    state = harness.begin_run("case_nested", "test", run_id="run_nested")
    hooks = ModelTranscriptHooks(harness, state)
    manager = SimpleNamespace(name="planner", model="deepseek-v4-flash")
    reviewer = SimpleNamespace(name="evidence_reviewer", model="deepseek-v4-flash")
    tool_context = SimpleNamespace(
        tool_call_id="delegate_1",
        tool_arguments='{"role":"evidence_reviewer"}',
    )
    tool = SimpleNamespace(name="delegate_agent")

    def response(text: str, request_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            output=[{"type": "message", "content": [{"type": "output_text", "text": text}]}],
            usage=Usage(requests=1, input_tokens=3, output_tokens=1, total_tokens=4),
            request_id=request_id,
            response_id=request_id,
        )

    _run(hooks.on_llm_start(SimpleNamespace(), manager, "manage", []))
    _run(hooks.on_llm_end(SimpleNamespace(), manager, response("delegate", "req_manager")))
    _run(hooks.on_tool_start(tool_context, manager, tool))
    _run(hooks.on_llm_start(SimpleNamespace(), reviewer, "review", []))
    _run(hooks.on_llm_end(SimpleNamespace(), reviewer, response("reviewed", "req_reviewer")))
    _run(hooks.on_agent_end(SimpleNamespace(), reviewer, "reviewed"))
    hooks.record_error(ValueError("post-provider nested schema error"))
    _run(hooks.on_tool_end(tool_context, manager, tool, "review result"))
    _run(hooks.on_llm_start(SimpleNamespace(), manager, "manage", []))
    _run(hooks.on_llm_end(SimpleNamespace(), manager, response("done", "req_final")))
    _run(hooks.on_agent_end(SimpleNamespace(), manager, "done"))

    events_path = store.resolve_case_path("case_nested", "traces/run_nested/events.jsonl")
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    provider_events = [event for event in events if event["kind"] == "provider_call"]
    assert [event["payload"]["call_number"] for event in provider_events] == [1, 2, 3]
    assert provider_events[0]["payload"]["status"] == "OK"
    assert provider_events[0]["payload"]["tools"][0]["result"] == "review result"
    assert provider_events[1]["payload"]["role"] == "evidence_reviewer"

    resumed = ModelTranscriptHooks(harness, state)
    _run(resumed.on_llm_start(SimpleNamespace(), manager, "resume", []))
    _run(resumed.on_llm_end(SimpleNamespace(), manager, response("resumed", "req_resume")))
    _run(resumed.on_agent_end(SimpleNamespace(), manager, "resumed"))
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    provider_events = [event for event in events if event["kind"] == "provider_call"]
    assert [event["payload"]["call_number"] for event in provider_events] == [1, 2, 3, 4]


def test_llm_client_uses_run_scoped_transcript_hooks(monkeypatch) -> None:
    class Output(BaseModel):
        ok: bool

    captured: dict[str, object] = {}

    def fake_run_agent_sync(*_args, **kwargs):
        captured["hooks"] = kwargs.get("hooks")
        return SimpleNamespace(final_output=Output(ok=True), raw_responses=[])

    monkeypatch.setattr("app.runtime.agents_sdk.run_agent_sync", fake_run_agent_sync)
    client = LlmClient(Settings(llm_api_key="test-only", llm_model="deepseek-v4-flash"))
    sentinel = object()
    token = client.bind_runtime_hooks(sentinel)
    try:
        output = client.complete_structured(
            role="summarizer",
            system_prompt="Summarize.",
            payload={"text": "visible"},
            model_type=Output,
            prompt_version="summarizer_v1",
        )
    finally:
        client.reset_runtime_hooks(token)

    assert output.ok is True
    assert captured["hooks"] is sentinel
