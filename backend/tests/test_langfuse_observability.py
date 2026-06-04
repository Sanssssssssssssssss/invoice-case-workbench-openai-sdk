from __future__ import annotations

import json
import sys
from typing import Any

import pytest

from app.config import Settings
from app.llm import LlmClient
from app.observability.langfuse_tracer import (
    LangfuseTracer,
    build_langfuse_input,
    cost_details,
    generation_hash_metadata,
    generation_input,
    generation_output,
)


class FakeObservation:
    def __init__(self, client: "FakeLangfuseClient", *, name: str, as_type: str, model: str = "") -> None:
        self.client = client
        self.name = name
        self.as_type = as_type
        self.model = model
        self.id = f"obs_{len(client.started) + 1}"
        self.trace_id = "trace_fake_001"
        self.updates: list[dict[str, Any]] = []
        self.ended = False

    def __enter__(self) -> "FakeObservation":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.end()

    def update(self, **kwargs: Any) -> "FakeObservation":
        self.updates.append(kwargs)
        return self

    def end(self) -> None:
        self.ended = True


class FakePrompt:
    def __init__(self, *, name: str, prompt: str, version: int = 1) -> None:
        self.name = name
        self.prompt = prompt
        self.version = version


class FakeLangfuseClient:
    def __init__(self) -> None:
        self.started: list[FakeObservation] = []
        self.scores: list[dict[str, Any]] = []
        self.prompts: dict[str, FakePrompt] = {}
        self.created_prompts: list[dict[str, Any]] = []
        self.flushed = False

    def start_as_current_observation(self, **kwargs: Any) -> FakeObservation:
        obs = FakeObservation(
            self,
            name=kwargs.get("name", ""),
            as_type=kwargs.get("as_type", "span"),
            model=kwargs.get("model", ""),
        )
        obs.update(**{key: value for key, value in kwargs.items() if key not in {"name", "as_type", "model"}})
        self.started.append(obs)
        return obs

    def get_trace_url(self, *, trace_id: str | None = None) -> str:
        return f"https://cloud.langfuse.com/project/fake/traces/{trace_id or ''}"

    def score_current_trace(self, **kwargs: Any) -> None:
        self.scores.append(kwargs)

    def flush(self) -> None:
        self.flushed = True

    def get_prompt(self, name: str, **_: Any) -> FakePrompt:
        if name not in self.prompts:
            raise KeyError(name)
        return self.prompts[name]

    def create_prompt(self, **kwargs: Any) -> FakePrompt:
        version = len([item for item in self.created_prompts if item.get("name") == kwargs["name"]]) + 1
        prompt = FakePrompt(name=kwargs["name"], prompt=kwargs["prompt"], version=version)
        self.prompts[kwargs["name"]] = prompt
        self.created_prompts.append(kwargs)
        return prompt


def test_langfuse_disabled_does_not_import_sdk() -> None:
    sys.modules.pop("langfuse", None)
    tracer = LangfuseTracer.from_settings(Settings(enable_langfuse=False))

    assert not tracer.enabled()
    assert "langfuse" not in sys.modules


def test_langfuse_no_key_is_noop() -> None:
    tracer = LangfuseTracer.from_settings(Settings(enable_langfuse=True, langfuse_public_key="", langfuse_secret_key=""))

    assert not tracer.enabled()
    assert "LANGFUSE_PUBLIC_KEY" in tracer.warning


def test_langfuse_turn_spans_generation_scores_and_flush() -> None:
    client = FakeLangfuseClient()
    tracer = LangfuseTracer(client=client, enabled=True, capture_payloads="summary")

    with tracer.start_turn(
        case_id="case_trace",
        run_id="run_trace",
        session_id="case_trace:main",
        message_summary="review invoice",
        max_steps=10,
        model="fake-model",
        workspace_root_hash="abc123",
    ) as turn:
        assert turn.status == "enabled"
        assert turn.trace_id == "trace_fake_001"
        assert turn.trace_url.endswith("trace_fake_001")
        with tracer.span("action.call_tool.read_case_state", input={"action": "call_tool"}) as span:
            span.update(output={"ok": True})
        with tracer.generation("llm.planner", model="fake-model", input={"payload_preview": "hello"}) as gen:
            gen.update(output={"raw_response_preview": "{}"}, usage_details={"input": 1, "output": 2})
        tracer.finish_turn(
            final_answer="done",
            case_status="ready_for_report",
            step_count=3,
            phase="finalized",
            feedback_summary={"ok": 1},
            report_paths=["reports/final_report.pdf"],
            guard_retry_count=0,
        )

    assert client.flushed
    assert [obs.name for obs in client.started] == [
        "invoice_agent.turn",
        "action.call_tool.read_case_state",
        "llm.planner",
    ]
    assert [obs.as_type for obs in client.started] == ["span", "span", "generation"]
    assert {score["name"] for score in client.scores} >= {
        "final_answer_present",
        "report_generated",
        "case_ready_for_report",
        "guard_retry_count",
    }


def test_generation_payload_modes_do_not_leak_full_by_default() -> None:
    payload = {"secret": "DO NOT LEAK FULL PAYLOAD", "path": r"C:\Users\ROG\WeChat Files\invoice.pdf"}
    summary_input = generation_input("planner", "v1", "SYSTEM PROMPT", payload, mode="summary")
    full_input = generation_input("planner", "v1", "SYSTEM PROMPT", payload, mode="full")
    summary_output = generation_output('{"action":"final_answer"}', parsed_schema="SupervisorDecision", mode="summary")
    metadata = generation_hash_metadata("SYSTEM PROMPT", payload)

    assert "payload_preview" not in summary_input
    assert isinstance(summary_input["payload"], dict)
    assert summary_input["prompt_version"] == "v1"
    assert summary_input["payload"]["role"] == "planner"
    assert "C:\\Users" not in json.dumps(summary_input, ensure_ascii=False, default=str)
    assert full_input["raw_payload_local_only"] is True
    assert full_input["payload"]["full_payload_summary_only"]["path"]["path_hash"]
    assert metadata["prompt_sha256"]
    assert metadata["payload_sha256"]
    assert metadata["raw_payload_local_only"] is True
    assert summary_output == {
        "parsed": {"action": "final_answer"},
        "raw_preview": '{"action":"final_answer"}',
        "schema": "SupervisorDecision",
    }


def test_role_specific_langfuse_inputs_are_structured() -> None:
    planner = build_langfuse_input(
        role="planner",
        mode="summary",
        payload={
            "user_message": "生成报告",
            "context_pack": {
                "case_state": {"case_id": "case_1", "status": "ready_for_report", "requirements": [{"id": "invoice"}]},
                "recent_observations": [{"kind": "tool", "name": "write_case_patch", "summary": "case updated"}],
                "memory_hints": [{"text": "中文报告"}],
                "report_paths": {"pdf_path": "reports/final.pdf"},
            },
        },
    )
    compactor = build_langfuse_input(
        role="session_compactor",
        mode="summary",
        payload={
            "case_snapshot": {"case_id": "case_2", "status": "new", "evidence_items": []},
            "reason": "PreRunContextBudget",
            "estimated_context_chars": 207158,
            "context_char_limit": 200000,
            "turns_to_compact": [
                {
                    "turn_id": "turn_001",
                    "user_message": "批一下报表",
                    "attachments": [{"name": "invoice.pdf", "path": r"D:\Program Files (x86)\WeChat Files\invoice.pdf"}],
                }
            ],
        },
    )
    reviewer = build_langfuse_input(
        role="evidence_reviewer",
        mode="summary",
        payload={
            "mode": "extract",
            "case_state": {"case_id": "case_3", "requirements": [{"id": "invoice_number", "status": "missing"}]},
            "attachment_context": [{"name": "invoice.pdf"}],
            "extraction_result": {"extracted_fields": {"invoice_number": "INV-1"}, "preview_paths": ["page.png"]},
        },
    )

    assert planner["user_message"] == "生成报告"
    assert planner["case"]["case_id"] == "case_1"
    assert planner["recent_observations"][0]["name"] == "write_case_patch"
    assert compactor["compaction"]["reason"] == "PreRunContextBudget"
    assert compactor["turns_to_compact"][0]["attachments"][0]["path_hash"]
    assert "D:\\Program" not in json.dumps(compactor, ensure_ascii=False, default=str)
    assert reviewer["mode"] == "extract"
    assert reviewer["field_count"] == 1


def test_cost_details_uses_configured_provider_rates() -> None:
    result = cost_details(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "prompt_tokens_details": {"cached_tokens": 200},
        },
        input_cost_per_1m=1.0,
        output_cost_per_1m=2.0,
        cached_input_cost_per_1m=0.25,
    )

    assert result["input"] == 0.00085
    assert result["output"] == 0.001
    assert result["cached_input"] == 0.00005
    assert result["total"] == 0.00185


def test_managed_prompt_is_created_and_linked_to_generation() -> None:
    client = FakeLangfuseClient()
    tracer = LangfuseTracer(client=client, enabled=True, capture_payloads="summary")

    prompt = tracer.managed_prompt(
        role="planner",
        prompt_version="planner_v1",
        system_prompt="system prompt",
        model="fake-model",
    )
    assert prompt is client.prompts["invoice-agent/planner"]
    assert client.created_prompts[0]["labels"] == ["production"]
    assert client.created_prompts[0]["config"]["prompt_sha256"]

    with tracer.generation("llm.planner", model="fake-model", input={}, prompt=prompt):
        pass
    assert client.started[0].updates[0]["prompt"] is prompt


def test_llm_unavailable_records_generation_error() -> None:
    client = FakeLangfuseClient()
    tracer = LangfuseTracer(client=client, enabled=True, capture_payloads="summary")
    llm = LlmClient(Settings(llm_api_key=None, enable_langfuse=False))
    llm.set_tracer(tracer)

    with pytest.raises(RuntimeError):
        llm.complete_with_tools(role="planner", system_prompt="system", payload={"x": 1}, tools=[], prompt_version="v1")

    assert [obs.name for obs in client.started] == ["llm.planner"]
    serialized = json.dumps([obs.updates for obs in client.started], ensure_ascii=False, default=str)
    assert "llm_unavailable" in serialized
