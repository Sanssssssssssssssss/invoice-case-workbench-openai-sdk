from __future__ import annotations

from typing import Any

from app.context import ContextManager
from app.harness import HarnessRuntime, HarnessRunState
from app.llm import ModelCallRecord
from app.observability.langfuse_tracer import (
    LangfuseTracer,
    safe_tool_input,
    safe_tool_output,
)
from app.tools.catalog import ToolCatalog


class ToolRuntime:
    def __init__(
        self,
        tools: ToolCatalog,
        harness: HarnessRuntime,
        context: ContextManager,
        tracer: LangfuseTracer | None = None,
    ) -> None:
        self.tools = tools
        self.harness = harness
        self.context = context
        self.tracer = tracer or LangfuseTracer.disabled()

    def call(
        self,
        state: HarnessRunState,
        tool: str,
        payload: dict[str, Any],
        *,
        attachments: list[Any] | None = None,
        internal: bool = False,
    ) -> dict[str, Any]:
        capability = self._capability_metadata(tool)
        with self.tracer.span(
            f"tool.{tool}",
            input=safe_tool_input(tool, payload),
            metadata={"tool": tool, "capability": capability, "internal": internal},
            as_type="tool",
        ) as span:
            try:
                result = self.tools.call(tool, state.case_id, payload, attachments=attachments, run_state=state, internal=internal)
                self.harness.record_tool_call(state, tool, payload, result, capability=capability)
                observation = self.context.record_result(state, kind="tool", name=tool, result=result)
                _record_model_call_debug(self.harness, state, self.context.llm.calls)
                self.harness.record_observation(state, observation)
                self._post_tool_use(state, tool, result)
                span.update(output=safe_tool_output(result))
                return result
            except Exception as exc:
                span.update(
                    output=safe_tool_output({}, error=f"{type(exc).__name__}: {exc}"),
                    level="ERROR",
                    status_message=str(exc),
                )
                raise

    def call_and_record_error(self, state: HarnessRunState, tool: str, payload: dict[str, Any], exc: Exception) -> None:
        self.harness.record_tool_call(
            state,
            tool,
            payload,
            {},
            error=f"{type(exc).__name__}: {exc}",
            capability=self._capability_metadata(tool),
        )
        self.harness.record_observation(state, self.context.record_error(kind="tool", name=tool, exc=exc))

    def _post_tool_use(self, state: HarnessRunState, tool: str, result: dict[str, Any]) -> None:
        if tool == "read_attachment":
            self.harness.set_phase(state, "attachment_read")
        elif tool == "write_case_patch":
            self.harness.set_phase(state, "patch_written")
        elif tool in {"write_case_file", "render_pdf"}:
            self.harness.set_phase(state, "report_written")

    def _capability_metadata(self, tool: str) -> dict[str, Any]:
        try:
            return self.tools.capability_metadata(tool)
        except KeyError:
            return {}


def _record_model_call_debug(harness: HarnessRuntime, state: HarnessRunState, calls: list[ModelCallRecord]) -> None:
    harness.record_model_calls(
        state,
        [item.to_dict() for item in calls],
        [item.to_debug_dict() for item in calls],
    )
