from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Iterable

from agents.lifecycle import RunHooks
from pydantic import BaseModel

from app.harness import HarnessRunState, HarnessRuntime, utc_now
from app.runtime.context_partition import usage_from_result
from app.state.persistence import append_text


_HIDDEN_KEYS = {"chain_of_thought", "encrypted_content", "reasoning", "reasoning_content"}
_SENSITIVE_KEYS = {"access_token", "api_key", "apikey", "authorization"}
_SK_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}", re.I)
_BEARER_TOKEN = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.I)
_LABELED_TOKEN = re.compile(
    r"(?i)([\"']?\b(?:api[_-]?key|access[_-]?token|authorization)\b[\"']?\s*[:=]\s*)"
    r"(?:[\"']?(?:Bearer\s+)?)[^\"'\s,;}]+[\"']?"
)
_DROP = object()


@dataclass
class _ProviderCall:
    number: int
    role: str
    model: str
    prompt_version: str
    started_at: str
    system_prompt: str
    input_items: Any
    output_items: Any = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    provider_request_id: str = ""
    error: str = ""
    ended: bool = False


class ModelTranscriptHooks(RunHooks[Any]):
    """Record each visible provider call without affecting the agent run."""

    def __init__(
        self,
        harness: HarnessRuntime,
        state: HarnessRunState,
        *,
        prompt_version: str = "",
        transcript_name: str = "deepseek_calls.txt",
        secret_values: Iterable[str] = (),
    ) -> None:
        self.harness = harness
        self.state = state
        self.prompt_version = prompt_version
        self.transcript_path = (
            harness.store.ensure_case_dirs(state.case_id) / "traces" / state.run_id / transcript_name
        )
        self.secret_values = tuple(
            sorted({str(value) for value in secret_values if str(value)}, key=len, reverse=True)
        )
        self.call_count = _existing_call_count(
            harness.store.ensure_case_dirs(state.case_id) / "traces" / state.run_id / "events.jsonl"
        )
        self._next_to_write = self.call_count + 1
        self._active: list[_ProviderCall] = []
        self._ready: dict[int, _ProviderCall] = {}

    async def on_llm_start(
        self,
        context: Any,
        agent: Any,
        system_prompt: str | None,
        input_items: list[Any],
    ) -> None:
        if self._active:
            previous = self._active[-1]
            if previous.ended and not any(tool["result"] is None for tool in previous.tools):
                self._finish(previous)
        self.call_count += 1
        self._active.append(_ProviderCall(
            number=self.call_count,
            role=str(getattr(agent, "name", None) or "model"),
            model=_model_name(getattr(agent, "model", None)),
            prompt_version=self.prompt_version,
            started_at=utc_now(),
            system_prompt=str(system_prompt or ""),
            input_items=input_items,
        ))

    async def on_llm_end(self, context: Any, agent: Any, response: Any) -> None:
        call = self._current_call(agent)
        if call is None:
            return
        call.output_items = getattr(response, "output", []) or []
        call.usage = usage_from_result(type("Result", (), {"raw_responses": [response]})())
        call.provider_request_id = str(
            getattr(response, "request_id", None) or getattr(response, "response_id", None) or ""
        )
        call.ended = True

    async def on_tool_start(self, context: Any, agent: Any, tool: Any) -> None:
        call = self._current_call(agent)
        if call is None:
            return
        call.tools.append(
            {
                "tool_call_id": str(getattr(context, "tool_call_id", None) or ""),
                "name": str(
                    getattr(tool, "name", None)
                    or getattr(context, "tool_name", None)
                    or type(tool).__name__
                ),
                "arguments": getattr(context, "tool_arguments", None),
                "result": None,
            }
        )

    async def on_tool_end(self, context: Any, agent: Any, tool: Any, result: str) -> None:
        call = self._current_call(agent)
        if call is None:
            return
        call_id = str(getattr(context, "tool_call_id", None) or "")
        for record in reversed(call.tools):
            if record["result"] is None and (not call_id or record["tool_call_id"] == call_id):
                record["result"] = result
                return
        call.tools.append(
            {
                "tool_call_id": call_id,
                "name": str(
                    getattr(tool, "name", None)
                    or getattr(context, "tool_name", None)
                    or type(tool).__name__
                ),
                "arguments": getattr(context, "tool_arguments", None),
                "result": result,
            }
        )

    async def on_agent_end(self, context: Any, agent: Any, output: Any) -> None:
        call = self._current_call(agent)
        if call is not None:
            self._finish(call)

    def record_error(self, exc: BaseException) -> None:
        call = next((item for item in reversed(self._active) if not item.ended), None)
        if call is None:
            return
        call.error = f"{type(exc).__name__}: {exc}"
        call.ended = True
        self._finish(call)

    def flush(self) -> None:
        for call in list(reversed(self._active)):
            self._finish(call)
        self._drain_ready()

    def _current_call(self, agent: Any) -> _ProviderCall | None:
        role = str(getattr(agent, "name", None) or "model")
        return next((call for call in reversed(self._active) if call.role == role), None)

    def _finish(self, call: _ProviderCall) -> None:
        if call in self._active:
            self._active.remove(call)
        self._ready[call.number] = call
        self._drain_ready()

    def _drain_ready(self) -> None:
        while self._next_to_write in self._ready:
            call = self._ready.pop(self._next_to_write)
            self._persist(call)
            self._next_to_write += 1

    def _persist(self, call: _ProviderCall) -> None:
        payload = self._sanitize(
            {
                "call_number": call.number,
                "role": call.role,
                "model": call.model,
                "prompt_version": call.prompt_version,
                "started_at": call.started_at,
                "status": "ERROR" if call.error else "OK",
                "system_prompt": call.system_prompt,
                "input": call.input_items,
                "output": call.output_items,
                "tools": call.tools,
                "usage": call.usage,
                "provider_request_id": call.provider_request_id,
                "error": call.error,
            }
        )
        if payload is _DROP or not isinstance(payload, dict):
            return
        try:
            self.harness.append_debug_event(
                self.state,
                kind="provider_call",
                name=call.role,
                payload=payload,
                summary=f"provider call {call.number:03d}: {call.role} / {call.model} / {payload['status']}",
                parent_event_id=self.state.last_action_event_id,
                caused_by_event_id=self.state.last_action_event_id,
            )
        except Exception:
            pass
        try:
            append_text(self.transcript_path, _format_call(payload))
        except Exception as exc:
            warning = self._sanitize(
                {"call_number": call.number, "error": f"{type(exc).__name__}: {exc}"}
            )
            try:
                self.harness.append_debug_event(
                    self.state,
                    kind="trace_warning",
                    name="model_transcript_write_failed",
                    payload=warning,
                    summary=f"deepseek_calls.txt write failed for provider call {call.number:03d}",
                )
            except Exception:
                pass

    def _sanitize(self, value: Any, *, key: str = "") -> Any:
        value = _jsonable(value)
        if key.lower() in _SENSITIVE_KEYS:
            return "[REDACTED]"
        if isinstance(value, str):
            return self._redact(value)
        if isinstance(value, dict):
            item_type = str(value.get("type") or "").lower()
            if item_type == "reasoning" or item_type.endswith(".reasoning"):
                return _DROP
            result: dict[str, Any] = {}
            for item_key, item_value in value.items():
                normalized_key = str(item_key).lower()
                if normalized_key in _HIDDEN_KEYS:
                    continue
                visible = self._sanitize(item_value, key=str(item_key))
                if visible is not _DROP:
                    result[str(item_key)] = visible
            return result
        if isinstance(value, (list, tuple)):
            result = []
            for item in value:
                visible = self._sanitize(item)
                if visible is not _DROP:
                    result.append(visible)
            return result
        return value

    def _redact(self, value: str) -> str:
        for secret in self.secret_values:
            value = value.replace(secret, "[REDACTED]")
        value = _SK_TOKEN.sub("[REDACTED]", value)
        value = _BEARER_TOKEN.sub("Bearer [REDACTED]", value)
        return _LABELED_TOKEN.sub(lambda match: f"{match.group(1)}[REDACTED]", value)


def _model_name(model: Any) -> str:
    if isinstance(model, str):
        return model
    return str(getattr(model, "model", None) or getattr(model, "model_name", None) or type(model).__name__)


def _existing_call_count(events_path: Any) -> int:
    try:
        lines = events_path.read_text(encoding="utf-8").splitlines()
    except (OSError, TypeError):
        return 0
    maximum = 0
    for line in lines:
        try:
            row = json.loads(line)
            if row.get("kind") == "provider_call" and isinstance(row.get("payload"), dict):
                maximum = max(maximum, int(row["payload"].get("call_number") or 0))
        except (TypeError, ValueError, AttributeError):
            continue
    return maximum


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json")
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        return {
            str(key): _jsonable(item)
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }
    return str(value)


def _pretty(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _format_call(payload: dict[str, Any]) -> str:
    tools = payload.get("tools") or []
    return "\n".join(
        [
            f"调用 {int(payload['call_number']):03d} | {payload['role']} | {payload['model']} | {payload['status']}",
            f"时间: {payload['started_at']}",
            f"Prompt 版本: {payload.get('prompt_version') or '-'}",
            f"Provider request: {payload.get('provider_request_id') or '-'}",
            "",
            "【SYSTEM / 问了什么】",
            _pretty(payload.get("system_prompt") or ""),
            "",
            "【INPUT / 给了什么】",
            _pretty(payload.get("input") or []),
            "",
            "【OUTPUT / 模型回答】",
            _pretty(payload.get("output") or []),
            "",
            "【TOOLS / 调用了什么】",
            _pretty(tools) if tools else "（无）",
            "",
            "【USAGE / ERROR】",
            _pretty({"usage": payload.get("usage") or {}, "error": payload.get("error") or ""}),
            "",
            "=" * 80,
            "",
        ]
    )
