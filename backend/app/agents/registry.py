from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from agents import Agent, AgentOutputSchema, FunctionTool, ModelSettings
from agents.run_context import RunContextWrapper
from agents.tool_context import ToolContext

from app.agents.capabilities import ROLE_CAPABILITIES, RoleCapability, role_capability
from app.agents.thinking import model_extra_body_for_thinking, role_thinking_type, temperature_for_thinking
from app.agents.patch_builder.agent import SYSTEM_PROMPT as CASE_PATCH_WRITER_PROMPT
from app.agents.evidence_reviewer.agent import SYSTEM_PROMPT as EVIDENCE_REVIEWER_PROMPT
from app.agents.materials_advisor.agent import SYSTEM_PROMPT as MATERIALS_ADVISOR_PROMPT
from app.agents.report_writer.agent import SYSTEM_PROMPT as REPORT_WRITER_PROMPT
from app.llm import LlmClient, ModelCallRecord
from app.observability.langfuse_tracer import (
    cost_details,
    generation_hash_metadata,
    generation_input,
    generation_output,
    usage_details,
)
from app.runtime.agents_sdk import build_run_config
from app.runtime.reasoning_capture import extract_reasoning_from_result


_ROLE_PROMPTS = {
    "materials_advisor": MATERIALS_ADVISOR_PROMPT,
    "evidence_reviewer": EVIDENCE_REVIEWER_PROMPT,
    "case_patch_writer": CASE_PATCH_WRITER_PROMPT,
    "report_writer": REPORT_WRITER_PROMPT,
}


class RoleRegistry:
    def __init__(self, llm: LlmClient | None = None) -> None:
        self.llm = llm or LlmClient()

    def call(self, role: str, payload: dict[str, Any], *, on_stream: Any | None = None) -> dict[str, Any]:
        capability = self.capability(role)
        system_prompt = self.prompt(role)
        prompt_version = capability.prompt_version
        input_text = json.dumps(payload, ensure_ascii=False, default=str)
        input_preview = input_text[:1400]
        model = self.llm.settings.llm_model
        timeout_seconds = self.llm.settings.timeout_for_role(role)
        thinking_type = role_thinking_type(role, payload, self.llm.settings.llm_thinking_type)
        started = time.perf_counter()
        runtime = "agent_as_tool"
        trace_metadata = {
            "role": role,
            "prompt_version": prompt_version,
            "temperature": temperature_for_thinking(model, self.llm.settings.llm_temperature, thinking_type),
            "thinking_type": thinking_type,
            "provider": self.llm.settings.llm_provider,
            "base_url": self.llm.settings.llm_base_url,
            "schema": capability.output_model.__name__,
            "runtime": runtime,
            "agent_as_tool": True,
            "timeout_seconds": timeout_seconds,
            **generation_hash_metadata(system_prompt, payload),
        }
        managed_prompt = self.llm.tracer.managed_prompt(
            role=role,
            prompt_version=prompt_version,
            system_prompt=system_prompt,
            model=model,
        )
        run_config = build_run_config(
            self.llm.settings,
            workflow_name=f"invoice_agent.{role}",
            trace_metadata={
                "role": role,
                "prompt_version": prompt_version,
                "payload_sha256": trace_metadata["payload_sha256"],
                "runtime": runtime,
                "agent_as_tool": True,
                "timeout_seconds": timeout_seconds,
            },
            timeout_seconds=timeout_seconds,
        )
        with self.llm.tracer.generation(
            f"llm.{role}",
            model=model,
            input=generation_input(role, prompt_version, system_prompt, payload, mode=self.llm.tracer.capture_payloads),
            metadata=trace_metadata,
            prompt=managed_prompt,
        ) as generation:
            if not self.llm.available:
                record = ModelCallRecord(
                    role=role,
                    model=model,
                    prompt_version=prompt_version,
                    input_preview=input_preview,
                    output_preview="",
                    error="llm_unavailable",
                    system_prompt=system_prompt,
                    payload=payload,
                    latency_ms=_elapsed_ms(started),
                    runtime=runtime,
                )
                self.llm.calls.append(record)
                generation.update(
                    output=generation_output("", error="llm_unavailable", mode=self.llm.tracer.capture_payloads),
                    metadata={"latency_ms": record.latency_ms, "runtime": runtime, "agent_as_tool": True},
                    level="ERROR",
                    status_message="llm_unavailable",
                )
                raise RuntimeError("LLM_API_KEY is required for specialist agent-as-tool output.")
            try:
                tool = self.as_tool(
                    role,
                    run_config=run_config,
                    started=started,
                    input_preview=input_preview,
                    payload=payload,
                    generation=generation,
                    thinking_type=thinking_type,
                    on_stream=on_stream,
                )
                raw_tool_input = json.dumps({"input": input_text}, ensure_ascii=False)
                tool_context = (
                    ToolContext(
                        context={"role": role},
                        tool_name=role,
                        tool_call_id=f"{role}_manual_call",
                        tool_arguments=raw_tool_input,
                        run_config=run_config,
                    )
                    if on_stream is not None
                    else RunContextWrapper(context={"role": role})
                )
                output_text = _await(tool.on_invoke_tool(tool_context, raw_tool_input))
                data = json.loads(output_text) if isinstance(output_text, str) else output_text
                parsed = capability.output_model.model_validate(data)
                return parsed.model_dump()
            except Exception as exc:
                record = ModelCallRecord(
                    role=role,
                    model=model,
                    prompt_version=prompt_version,
                    input_preview=input_preview,
                    output_preview="",
                    error=f"{type(exc).__name__}: {exc}",
                    system_prompt=system_prompt,
                    payload=payload,
                    latency_ms=_elapsed_ms(started),
                    runtime=runtime,
                )
                self.llm.calls.append(record)
                generation.update(
                    output=generation_output("", error=record.error, mode=self.llm.tracer.capture_payloads),
                    metadata={"latency_ms": record.latency_ms, "schema": capability.output_model.__name__, "runtime": runtime, "agent_as_tool": True},
                    level="ERROR",
                    status_message=record.error,
                )
                raise
            finally:
                _close_run_config_client(run_config)

    def as_tool(
        self,
        role: str,
        *,
        run_config: Any,
        started: float,
        input_preview: str,
        payload: dict[str, Any],
        generation: Any,
        thinking_type: str | None = None,
        on_stream: Any | None = None,
    ) -> FunctionTool:
        capability = self.capability(role)
        agent = self.agent(role, thinking_type=thinking_type)

        async def output_extractor(result: Any) -> str:
            parsed = result.final_output
            if not isinstance(parsed, capability.output_model):
                parsed = capability.output_model.model_validate(parsed)
            raw_response = parsed.model_dump_json()
            usage = _usage_from_result(result)
            reasoning = extract_reasoning_from_result(result)
            record = ModelCallRecord(
                role=role,
                model=self.llm.settings.llm_model,
                prompt_version=capability.prompt_version,
                input_preview=input_preview,
                output_preview=raw_response[:1400],
                system_prompt=self.prompt(role),
                payload=payload,
                raw_response=raw_response,
                usage=usage,
                latency_ms=_elapsed_ms(started),
                content_chars=len(raw_response),
                runtime="agent_as_tool",
                reasoning_excerpt=reasoning.text if reasoning else "",
                reasoning_chars=reasoning.chars if reasoning else 0,
                reasoning_chunks=reasoning.chunks if reasoning else 0,
                thinking_type=str(thinking_type or "disabled"),
                reasoning_source=reasoning.source if reasoning else "",
            )
            self.llm.calls.append(record)
            generation.update(
                output=generation_output(raw_response, parsed_schema=capability.output_model.__name__, mode=self.llm.tracer.capture_payloads),
                usage_details=usage_details(usage),
                cost_details=cost_details(
                    usage,
                    input_cost_per_1m=self.llm.settings.llm_input_cost_per_1m,
                    output_cost_per_1m=self.llm.settings.llm_output_cost_per_1m,
                    cached_input_cost_per_1m=self.llm.settings.llm_cached_input_cost_per_1m,
                ),
                metadata={"latency_ms": record.latency_ms, "runtime": "agent_as_tool", "agent_as_tool": True},
            )
            return raw_response

        return agent.as_tool(
            tool_name=role,
            tool_description=f"Run specialist agent {role} and return {capability.output_model.__name__}.",
            custom_output_extractor=output_extractor,
            failure_error_function=None,
            run_config=run_config,
            max_turns=2,
            on_stream=on_stream,
            include_input_schema=False,
        )

    def agent(self, role: str, *, thinking_type: str | None = None) -> Agent:
        capability = self.capability(role)
        model = self.llm.settings.llm_model
        selected_thinking_type = str(thinking_type or "disabled")
        return Agent(
            name=role,
            instructions=self.prompt(role),
            model=model,
            model_settings=ModelSettings(
                temperature=temperature_for_thinking(model, self.llm.settings.llm_temperature, selected_thinking_type),
                extra_body=model_extra_body_for_thinking(model, selected_thinking_type),
            ),
            output_type=AgentOutputSchema(capability.output_model, strict_json_schema=False),
        )

    def prompt(self, role: str) -> str:
        return _ROLE_PROMPTS.get(role, "")

    def prompt_file(self, role: str) -> str:
        try:
            return self.capability(role).prompt_file
        except KeyError:
            return ""

    def prompt_version(self, role: str) -> str:
        return self.capability(role).prompt_version

    def capability(self, role: str) -> RoleCapability:
        if role not in ROLE_CAPABILITIES:
            raise KeyError(f"Unknown role: {role}")
        return role_capability(role)

    def trace_metadata(self, role: str) -> dict[str, Any]:
        metadata = self.capability(role).trace_metadata()
        metadata["runtime"] = "agent_as_tool"
        metadata["agent_as_tool"] = True
        return metadata

    @property
    def role_names(self) -> tuple[str, ...]:
        return tuple(ROLE_CAPABILITIES.keys())


def _usage_from_result(result: Any) -> dict[str, Any]:
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for response in getattr(result, "raw_responses", []) or []:
        usage = getattr(response, "usage", None)
        if hasattr(usage, "model_dump"):
            usage = usage.model_dump()
        if not isinstance(usage, dict):
            continue
        totals["prompt_tokens"] += int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0)
        totals["completion_tokens"] += int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0)
        totals["total_tokens"] += int(usage.get("total_tokens", 0) or 0)
    return {key: value for key, value in totals.items() if value}


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def _await(value: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(value)
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(lambda: asyncio.run(value)).result()


def _close_run_config_client(run_config: Any) -> None:
    if not getattr(run_config, "_invoice_close_openai_client", True):
        return
    client = getattr(run_config, "_invoice_openai_client", None)
    if client is not None:
        try:
            _await(client.close())
        except RuntimeError as exc:
            if "Event loop is closed" not in str(exc):
                raise
