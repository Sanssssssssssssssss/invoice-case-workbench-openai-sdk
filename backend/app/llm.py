from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel

from app.config import Settings, get_settings
from app.observability.langfuse_tracer import (
    LangfuseTracer,
    cost_details,
    generation_hash_metadata,
    generation_input,
    generation_output,
    usage_details,
)


THINKING_EXCERPT_CHARS = 1400
THINKING_EMIT_CHARS = 360
THINKING_EMIT_CHUNKS = 18
ThinkingRecorder = Callable[[dict[str, Any]], None]


@dataclass
class ModelCallRecord:
    role: str
    model: str
    prompt_version: str
    input_preview: str
    output_preview: str
    error: str = ""
    system_prompt: str = ""
    payload: Any = None
    raw_response: str = ""
    usage: dict[str, Any] | None = None
    latency_ms: float | None = None
    provider_request_id: str = ""
    finish_reason: str = ""
    schema_validation_error: str = ""
    schema_retry_attempted: bool = False
    retry_of: str = ""
    thinking_enabled: bool = False
    reasoning_excerpt: str = ""
    reasoning_chars: int = 0
    reasoning_chunks: int = 0
    content_chars: int = 0
    recovered_by: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "input_preview": self.input_preview,
            "output_preview": self.output_preview,
            "schema_validation_error": self.schema_validation_error,
            "schema_retry_attempted": self.schema_retry_attempted,
            "retry_of": self.retry_of,
            "error": self.error,
            "thinking_enabled": self.thinking_enabled,
            "reasoning_excerpt": self.reasoning_excerpt,
            "reasoning_chars": self.reasoning_chars,
            "reasoning_chunks": self.reasoning_chunks,
            "content_chars": self.content_chars,
            "recovered_by": self.recovered_by,
        }

    def to_debug_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "system_prompt": self.system_prompt,
            "payload": self.payload,
            "raw_response": self.raw_response,
            "input_preview": self.input_preview,
            "output_preview": self.output_preview,
            "usage": self.usage or {},
            "latency_ms": self.latency_ms,
            "provider_request_id": self.provider_request_id,
            "finish_reason": self.finish_reason,
            "schema_validation_error": self.schema_validation_error,
            "schema_retry_attempted": self.schema_retry_attempted,
            "retry_of": self.retry_of,
            "error": self.error,
            "thinking_enabled": self.thinking_enabled,
            "reasoning_excerpt": self.reasoning_excerpt,
            "reasoning_chars": self.reasoning_chars,
            "reasoning_chunks": self.reasoning_chunks,
            "content_chars": self.content_chars,
            "recovered_by": self.recovered_by,
        }


@dataclass
class NativeToolMessage:
    content: str = ""
    tool_calls: list[dict[str, Any]] | None = None
    additional_kwargs: dict[str, Any] | None = None
    response_metadata: dict[str, Any] | None = None
    usage_metadata: dict[str, Any] | None = None


class LlmClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.calls: list[ModelCallRecord] = []
        self.tracer = LangfuseTracer.disabled()
        self.thinking_recorder: ThinkingRecorder | None = None

    def set_tracer(self, tracer: LangfuseTracer) -> None:
        self.tracer = tracer

    def set_thinking_recorder(self, recorder: ThinkingRecorder | None) -> None:
        self.thinking_recorder = recorder

    @property
    def available(self) -> bool:
        return bool(self.settings.llm_api_key)

    def complete_structured(
        self,
        *,
        role: str,
        system_prompt: str,
        payload: dict[str, Any],
        model_type: type[BaseModel],
        prompt_version: str,
        model: str | None = None,
    ) -> BaseModel:
        from agents import Agent, AgentOutputSchema, ModelSettings, Runner

        input_preview = json.dumps(payload, ensure_ascii=False, default=str)[:1400]
        selected_model = model or self.settings.llm_model
        timeout_seconds = self.settings.timeout_for_role(role)
        started = time.perf_counter()
        trace_input = generation_input(
            role,
            prompt_version,
            system_prompt,
            payload,
            mode=self.tracer.capture_payloads,
        )
        trace_metadata = {
            "role": role,
            "prompt_version": prompt_version,
            "temperature": self._temperature(selected_model),
            "provider": self.settings.llm_provider,
            "base_url": self.settings.llm_base_url,
            "schema": model_type.__name__,
            "runtime": "openai_agents_sdk",
            "timeout_seconds": timeout_seconds,
            **generation_hash_metadata(system_prompt, payload),
        }
        managed_prompt = self.tracer.managed_prompt(
            role=role,
            prompt_version=prompt_version,
            system_prompt=system_prompt,
            model=selected_model,
        )
        with self.tracer.generation(
            f"llm.{role}",
            model=selected_model,
            input=trace_input,
            metadata=trace_metadata,
            prompt=managed_prompt,
        ) as generation:
            if not self.available:
                record = ModelCallRecord(
                    role=role,
                    model=selected_model,
                    prompt_version=prompt_version,
                    input_preview=input_preview,
                    output_preview="",
                    error="llm_unavailable",
                    system_prompt=system_prompt,
                    payload=payload,
                    latency_ms=_elapsed_ms(started),
                )
                self.calls.append(record)
                generation.update(
                    output=generation_output("", error="llm_unavailable", mode=self.tracer.capture_payloads),
                    metadata={"latency_ms": record.latency_ms},
                    level="ERROR",
                    status_message="llm_unavailable",
                )
                raise RuntimeError("LLM_API_KEY is required for structured agent output.")
            try:
                agent = Agent(
                    name=role,
                    instructions=system_prompt,
                    model=selected_model,
                    model_settings=ModelSettings(
                        temperature=self._temperature(selected_model),
                        extra_body=_model_extra_body(selected_model, self.settings.llm_thinking_type),
                    ),
                    output_type=AgentOutputSchema(model_type, strict_json_schema=False),
                )
                from app.runtime.agents_sdk import run_agent_sync

                result = run_agent_sync(
                    agent,
                    json.dumps(payload, ensure_ascii=False, default=str),
                    run_config=self._run_config(
                        workflow_name=f"invoice_agent.{role}",
                        trace_metadata={
                            "role": role,
                            "prompt_version": prompt_version,
                            "payload_sha256": trace_metadata["payload_sha256"],
                            "timeout_seconds": timeout_seconds,
                        },
                        timeout_seconds=timeout_seconds,
                    ),
                )
                parsed = result.final_output
                if not isinstance(parsed, model_type):
                    parsed = model_type.model_validate(parsed)
                raw_response = parsed.model_dump_json()
                usage = _usage_from_result(result)
                record = ModelCallRecord(
                    role=role,
                    model=selected_model,
                    prompt_version=prompt_version,
                    input_preview=input_preview,
                    output_preview=raw_response[:1400],
                    system_prompt=system_prompt,
                    payload=payload,
                    raw_response=raw_response,
                    usage=usage,
                    latency_ms=_elapsed_ms(started),
                    content_chars=len(raw_response),
                )
                self.calls.append(record)
                generation.update(
                    output=generation_output(
                        raw_response,
                        parsed_schema=model_type.__name__,
                        mode=self.tracer.capture_payloads,
                    ),
                    usage_details=usage_details(usage),
                    cost_details=cost_details(
                        usage,
                        input_cost_per_1m=self.settings.llm_input_cost_per_1m,
                        output_cost_per_1m=self.settings.llm_output_cost_per_1m,
                        cached_input_cost_per_1m=self.settings.llm_cached_input_cost_per_1m,
                    ),
                    metadata={"latency_ms": record.latency_ms, "runtime": "openai_agents_sdk"},
                )
                return parsed
            except Exception as exc:
                record = ModelCallRecord(
                    role=role,
                    model=selected_model,
                    prompt_version=prompt_version,
                    input_preview=input_preview,
                    output_preview="",
                    error=f"{type(exc).__name__}: {exc}",
                    system_prompt=system_prompt,
                    payload=payload,
                    latency_ms=_elapsed_ms(started),
                )
                self.calls.append(record)
                generation.update(
                    output=generation_output("", error=record.error, mode=self.tracer.capture_payloads),
                    metadata={"latency_ms": record.latency_ms, "schema": model_type.__name__},
                    level="ERROR",
                    status_message=record.error,
                )
                raise

    def complete_with_tools(
        self,
        *,
        role: str,
        system_prompt: str,
        payload: dict[str, Any],
        tools: list[dict[str, Any]],
        prompt_version: str,
        model: str | None = None,
    ) -> NativeToolMessage:
        """Compatibility helper for older tests and prompt probes."""
        from openai import OpenAI

        selected_model = model or self.settings.llm_model
        timeout_seconds = self.settings.timeout_for_role(role)
        input_preview = json.dumps(payload, ensure_ascii=False, default=str)[:1400]
        temperature = self._temperature(selected_model)
        started = time.perf_counter()
        trace_input = generation_input(
            role,
            prompt_version,
            system_prompt,
            payload,
            mode=self.tracer.capture_payloads,
        )
        trace_metadata = {
            "role": role,
            "prompt_version": prompt_version,
            "temperature": temperature,
            "provider": self.settings.llm_provider,
            "base_url": self.settings.llm_base_url,
            "schema": "native_tool_calls",
            "tool_count": len(tools),
            "runtime": "openai_sdk_compat",
            "timeout_seconds": timeout_seconds,
            **generation_hash_metadata(system_prompt, payload),
        }
        managed_prompt = self.tracer.managed_prompt(
            role=role,
            prompt_version=prompt_version,
            system_prompt=system_prompt,
            model=selected_model,
        )
        with self.tracer.generation(
            f"llm.{role}",
            model=selected_model,
            input=trace_input,
            metadata=trace_metadata,
            prompt=managed_prompt,
        ) as generation:
            if not self.available:
                record = ModelCallRecord(
                    role=role,
                    model=selected_model,
                    prompt_version=prompt_version,
                    input_preview=input_preview,
                    output_preview="",
                    error="llm_unavailable",
                    system_prompt=system_prompt,
                    payload=payload,
                    latency_ms=_elapsed_ms(started),
                )
                self.calls.append(record)
                generation.update(
                    output=generation_output("", error="llm_unavailable", mode=self.tracer.capture_payloads),
                    metadata={"latency_ms": record.latency_ms},
                    level="ERROR",
                    status_message="llm_unavailable",
                )
                raise RuntimeError("LLM_API_KEY is required for native tool calling.")
            try:
                client = OpenAI(
                    api_key=self.settings.llm_api_key,
                    base_url=self.settings.llm_base_url,
                    timeout=timeout_seconds,
                    max_retries=0,
                )
                kwargs: dict[str, Any] = {
                    "model": selected_model,
                    "temperature": temperature,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
                    ],
                    "tools": tools,
                    "tool_choice": "auto",
                }
                extra_body = _model_extra_body(selected_model, self.settings.llm_thinking_type)
                if extra_body:
                    kwargs["extra_body"] = extra_body
                response = client.chat.completions.create(**kwargs)
                choice = response.choices[0] if response.choices else None
                message = choice.message if choice else None
                content = str(getattr(message, "content", "") or "")
                reasoning = str(getattr(message, "reasoning_content", "") or "")
                tool_calls = []
                raw_tool_calls = []
                for call in getattr(message, "tool_calls", None) or []:
                    raw = call.model_dump() if hasattr(call, "model_dump") else {}
                    raw_tool_calls.append(raw)
                    function = getattr(call, "function", None)
                    name = str(getattr(function, "name", "") or "")
                    raw_args = str(getattr(function, "arguments", "") or "{}")
                    try:
                        args = json.loads(raw_args) if raw_args.strip() else {}
                    except json.JSONDecodeError:
                        args = {}
                    tool_calls.append(
                        {
                            "name": name,
                            "args": args if isinstance(args, dict) else {},
                            "id": str(getattr(call, "id", "") or name),
                            "type": "tool_call",
                        }
                    )
                usage = _model_dump(getattr(response, "usage", None))
                output_payload = {"content": content, "tool_calls": raw_tool_calls}
                record = ModelCallRecord(
                    role=role,
                    model=selected_model,
                    prompt_version=prompt_version,
                    input_preview=input_preview,
                    output_preview=json.dumps(output_payload, ensure_ascii=False, default=str)[:1400],
                    system_prompt=system_prompt,
                    payload=payload,
                    raw_response=json.dumps(output_payload, ensure_ascii=False, default=str),
                    usage=usage,
                    latency_ms=_elapsed_ms(started),
                    provider_request_id=str(getattr(response, "id", "") or ""),
                    finish_reason=str(getattr(choice, "finish_reason", "") or ""),
                    thinking_enabled=bool(reasoning),
                    reasoning_excerpt=_excerpt(reasoning),
                    reasoning_chars=len(reasoning),
                    reasoning_chunks=1 if reasoning else 0,
                    content_chars=len(content),
                )
                self.calls.append(record)
                if reasoning:
                    self._emit_thinking(
                        role=role,
                        model=selected_model,
                        prompt_version=prompt_version,
                        reasoning_text=reasoning,
                        reasoning_chunks=1,
                        content_started=bool(content or tool_calls),
                        status="completed",
                    )
                generation.update(
                    output=generation_output(
                        json.dumps(output_payload, ensure_ascii=False, default=str),
                        parsed_schema="native_tool_calls",
                        mode=self.tracer.capture_payloads,
                    ),
                    usage_details=usage_details(usage),
                    cost_details=cost_details(
                        usage,
                        input_cost_per_1m=self.settings.llm_input_cost_per_1m,
                        output_cost_per_1m=self.settings.llm_output_cost_per_1m,
                        cached_input_cost_per_1m=self.settings.llm_cached_input_cost_per_1m,
                    ),
                    metadata={
                        "provider_request_id": record.provider_request_id,
                        "finish_reason": record.finish_reason,
                        "latency_ms": record.latency_ms,
                        "runtime": "openai_sdk_compat",
                    },
                )
                return NativeToolMessage(
                    content=content,
                    tool_calls=tool_calls,
                    response_metadata={
                        "finish_reason": record.finish_reason,
                        "provider_request_id": record.provider_request_id,
                    },
                    usage_metadata=_usage_metadata(usage),
                )
            except Exception as exc:
                record = ModelCallRecord(
                    role=role,
                    model=selected_model,
                    prompt_version=prompt_version,
                    input_preview=input_preview,
                    output_preview="",
                    error=f"{type(exc).__name__}: {exc}",
                    system_prompt=system_prompt,
                    payload=payload,
                    latency_ms=_elapsed_ms(started),
                )
                self.calls.append(record)
                generation.update(
                    output=generation_output("", error=record.error, mode=self.tracer.capture_payloads),
                    metadata={"latency_ms": record.latency_ms, "schema": "native_tool_calls"},
                    level="ERROR",
                    status_message=record.error,
                )
                raise

    def _run_config(
        self,
        *,
        workflow_name: str,
        trace_metadata: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ):
        from app.runtime.agents_sdk import build_run_config

        return build_run_config(
            self.settings,
            workflow_name=workflow_name,
            trace_metadata=trace_metadata,
            timeout_seconds=timeout_seconds,
        )

    def _temperature(self, model_name: str | None = None) -> float:
        model = (model_name or self.settings.llm_model).lower()
        if model == "kimi-k2.5" and (self.settings.llm_thinking_type or "").lower() == "disabled":
            return 0.6
        if model.startswith("kimi-k2"):
            return 1.0
        return self.settings.llm_temperature

    def _native_structured_temperature(self, model_name: str | None = None) -> float:
        model = (model_name or self.settings.llm_model).lower()
        if model.startswith("kimi-k2"):
            return 0.6
        return self._temperature(model_name)

    def _complete_streaming(
        self,
        *,
        client: Any,
        kwargs: dict[str, Any],
        role: str,
        model: str,
        prompt_version: str,
    ) -> tuple[str, str, int, dict[str, Any], str]:
        streaming_kwargs = dict(kwargs)
        streaming_kwargs["stream"] = True
        streaming_kwargs["max_tokens"] = int(streaming_kwargs.get("max_tokens") or 1024 * 32)
        streaming_kwargs["extra_body"] = _merge_thinking_body(streaming_kwargs.get("extra_body"))
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        reasoning_chunks = 0
        last_emit_chars = 0
        last_emit_chunks = 0
        finish_reason = ""
        usage: dict[str, Any] = {}
        for chunk in client.chat.completions.create(**streaming_kwargs):
            chunk_usage = _model_dump(getattr(chunk, "usage", None))
            if chunk_usage:
                usage = chunk_usage
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            choice = choices[0]
            finish_reason = str(getattr(choice, "finish_reason", "") or finish_reason)
            delta = getattr(choice, "delta", None)
            if delta is None:
                continue
            reasoning = str(getattr(delta, "reasoning_content", "") or "")
            content = str(getattr(delta, "content", "") or "")
            if reasoning:
                reasoning_chunks += 1
                reasoning_parts.append(reasoning)
                reasoning_text = "".join(reasoning_parts)
                should_emit = (
                    len(reasoning_text) - last_emit_chars >= THINKING_EMIT_CHARS
                    or reasoning_chunks - last_emit_chunks >= THINKING_EMIT_CHUNKS
                )
                if should_emit:
                    self._emit_thinking(
                        role=role,
                        model=model,
                        prompt_version=prompt_version,
                        reasoning_text=reasoning_text,
                        reasoning_chunks=reasoning_chunks,
                        content_started=bool(content_parts),
                        status="streaming",
                    )
                    last_emit_chars = len(reasoning_text)
                    last_emit_chunks = reasoning_chunks
            if content:
                content_parts.append(content)
        reasoning_text = "".join(reasoning_parts)
        if reasoning_text:
            self._emit_thinking(
                role=role,
                model=model,
                prompt_version=prompt_version,
                reasoning_text=reasoning_text,
                reasoning_chunks=reasoning_chunks,
                content_started=True,
                status="completed",
            )
        return "".join(content_parts), reasoning_text, reasoning_chunks, usage, finish_reason

    def _emit_thinking(
        self,
        *,
        role: str,
        model: str,
        prompt_version: str,
        reasoning_text: str,
        reasoning_chunks: int,
        content_started: bool,
        status: str,
    ) -> None:
        if not self.thinking_recorder:
            return
        self.thinking_recorder(
            {
                "role": role,
                "model": model,
                "prompt_version": prompt_version,
                "reasoning_excerpt": _excerpt(reasoning_text),
                "reasoning_chars": len(reasoning_text),
                "reasoning_chunks": reasoning_chunks,
                "content_started": content_started,
                "status": status,
            }
        )


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def _use_responses_api(provider: str, base_url: str) -> bool:
    if str(provider or "").lower() != "openai":
        return False
    return str(base_url or "").rstrip("/") == "https://api.openai.com/v1"


def _model_extra_body(model_name: str, thinking_type: str | None) -> dict[str, Any] | None:
    model = str(model_name or "").lower()
    if model == "kimi-k2.5":
        return {"thinking": {"type": str(thinking_type or "disabled")}}
    return None


def _merge_thinking_body(extra_body: Any) -> dict[str, Any]:
    body = dict(extra_body) if isinstance(extra_body, dict) else {}
    thinking = dict(body.get("thinking")) if isinstance(body.get("thinking"), dict) else {}
    thinking["type"] = "enabled"
    body["thinking"] = thinking
    return body


def _runner_run_sync(runner: Any, *args: Any, **kwargs: Any) -> Any:
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return runner.run_sync(*args, **kwargs)
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(lambda: runner.run_sync(*args, **kwargs)).result()


def _usage_from_result(result: Any) -> dict[str, Any]:
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for response in getattr(result, "raw_responses", []) or []:
        usage = _model_dump(getattr(response, "usage", None))
        totals["prompt_tokens"] += int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0)
        totals["completion_tokens"] += int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0)
        totals["total_tokens"] += int(usage.get("total_tokens", 0) or 0)
    return {key: value for key, value in totals.items() if value}


def _model_dump(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump()
        except Exception:
            return {}
    if isinstance(value, dict):
        return value
    return {}


def _usage_metadata(usage: dict[str, Any]) -> dict[str, Any]:
    if not usage:
        return {}
    return {
        "input_tokens": int(usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }


def _excerpt(value: str, max_chars: int = THINKING_EXCERPT_CHARS) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head - 18
    return f"{text[:head].rstrip()}\n...[thinking trimmed]...\n{text[-tail:].lstrip()}"
