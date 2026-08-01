from __future__ import annotations

from types import SimpleNamespace

from app.api.read_models import normalize_trace_event
from app.config import Settings, get_settings
from app.harness import HarnessRuntime
from app.llm import LlmClient, ModelCallRecord
from app.observability.model_metrics import build_model_metrics, mark_first_model_output, mark_model_started, model_ttft_ms
from app.runtime.trace_recorder import TraceRecorder
from app.runtime.turn_runner import TurnRunner
from app.session_manager import SessionManager
from app.state.case_store import CaseStore


def test_model_call_record_exposes_flat_metrics() -> None:
    record = ModelCallRecord(
        role="planner",
        model="gpt-test",
        prompt_version="v1",
        input_preview="in",
        output_preview="out",
        usage={"prompt_tokens": 2000, "completion_tokens": 120, "total_tokens": 2120, "cached_tokens": 1500},
        latency_ms=123.45,
        ttft_ms=67.89,
        prompt_partition={
            "stable_prefix_hash": "stable",
            "dynamic_context_hash": "dynamic",
            "volatile_tail_hash": "volatile",
            "prompt_cache_key": "invoice_workbench:tenant:planner:v1:tools",
            "prompt_tokens": 2000,
            "cached_tokens": 1500,
            "cache_hit_ratio": 0.75,
        },
    )

    compact = record.to_dict()
    debug = record.to_debug_dict()

    assert compact["latency_ms"] == 123.45
    assert compact["ttft_ms"] == 67.89
    assert compact["prompt_tokens"] == 2000
    assert compact["completion_tokens"] == 120
    assert compact["total_tokens"] == 2120
    assert compact["cached_tokens"] == 1500
    assert compact["cache_hit_ratio"] == 0.75
    assert compact["prompt_cache_key"] == "invoice_workbench:tenant:planner:v1:tools"
    assert compact["prompt_partition"]["stable_prefix_hash"] == "stable"
    assert debug["usage"]["total_tokens"] == 2120
    assert debug["latency_ms"] == compact["latency_ms"]


def test_model_metrics_tolerate_missing_usage() -> None:
    metrics = build_model_metrics(
        usage=None,
        prompt_partition={"prompt_cache_key": "cache-key"},
        latency_ms=5,
        ttft_ms=None,
    )

    assert metrics["latency_ms"] == 5.0
    assert metrics["ttft_ms"] is None
    assert metrics["prompt_tokens"] is None
    assert metrics["cached_tokens"] is None
    assert metrics["cache_hit_ratio"] is None
    assert metrics["prompt_cache_key"] == "cache-key"


def test_trace_recorder_writes_model_metrics_summary(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases")
    llm = LlmClient(Settings(llm_api_key="test"))
    harness = HarnessRuntime(store)
    state = harness.begin_run("case_metrics", "test", run_id="run_metrics")
    llm.calls.append(
        ModelCallRecord(
            role="planner",
            model="gpt-test",
            prompt_version="v1",
            input_preview="in",
            output_preview="out",
            usage={"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100, "cached_tokens": 400},
            latency_ms=42,
            prompt_partition={"prompt_tokens": 1000, "cached_tokens": 400, "cache_hit_ratio": 0.4},
        )
    )
    state.observability["_model_timing"] = {"planner": {"started_perf": 10.0}}

    recorder = TraceRecorder(store=store, llm=llm, harness=harness, sessions=SessionManager(store, llm))
    recorder.record_model_call_debug(state)

    metrics = state.observability["model_metrics"]
    assert metrics["call_count"] == 1
    assert metrics["prompt_tokens"] == 1000
    assert metrics["cached_tokens"] == 400
    assert metrics["cache_hit_ratio"] == 0.4
    assert "_model_timing" not in state.compact_trace()["observability"]


def test_normalize_trace_event_reads_model_metric_fields() -> None:
    event = normalize_trace_event(
        {
            "kind": "model_call",
            "event_id": "evt_1",
            "run_id": "run_1",
            "case_id": "case_1",
            "seq": 1,
            "case_seq": 1,
            "payload": {
                "role": "planner",
                "latency_ms": 91.2,
                "prompt_tokens": 100,
                "completion_tokens": 25,
                "total_tokens": 125,
            },
        }
    )

    assert event.kind == "model"
    assert event.duration_ms == 91
    assert event.token_count == 125


def test_streaming_ttft_is_recorded_on_first_delta(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("INVOICE_AGENT_WORKSPACE_ROOT", str(tmp_path / "cases"))
    monkeypatch.setenv("INVOICE_AGENT_STORAGE_ROOT", str(tmp_path / "storage"))
    get_settings.cache_clear()
    store = CaseStore(tmp_path / "cases")
    runner = TurnRunner(store=store, llm=LlmClient(Settings(llm_api_key="test")))
    state = HarnessRuntime(store).begin_run("case_ttft", "test", run_id="run_ttft")

    mark_model_started(state.observability, "planner", now=10.0)
    mark_first_model_output(state.observability, "planner", now=10.321)
    assert model_ttft_ms(state.observability, "planner") == 321.0

    state.observability.clear()
    runner.record_sdk_stream_event(
        state,
        SimpleNamespace(type="agent_updated_stream_event", new_agent=SimpleNamespace(name="case_manager")),
    )
    runner.record_sdk_stream_event(
        state,
        SimpleNamespace(type="raw_response_event", data=SimpleNamespace(type="response.output_text.delta", delta="hello")),
    )

    assert model_ttft_ms(state.observability, "planner") is not None
