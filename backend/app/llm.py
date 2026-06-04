from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

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
            "content_chars": self.content_chars,
            "recovered_by": self.recovered_by,
        }


class LlmClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.calls: list[ModelCallRecord] = []
        self.tracer = LangfuseTracer.disabled()

    def set_tracer(self, tracer: LangfuseTracer) -> None:
        self.tracer = tracer

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
        from agents import Agent, AgentOutputSchema, ModelSettings

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


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def _model_extra_body(model_name: str, thinking_type: str | None) -> dict[str, Any] | None:
    model = str(model_name or "").lower()
    if model == "kimi-k2.5":
        return {"thinking": {"type": str(thinking_type or "disabled")}}
    return None


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
