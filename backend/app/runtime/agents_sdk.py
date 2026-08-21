from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from agents import AgentOutputSchema, RunConfig, Runner
from agents.models.openai_provider import OpenAIProvider
from openai import AsyncOpenAI

from app.config import Settings


_SHARED_CLIENTS_ENABLED = False
_SHARED_CLIENTS: dict[tuple[str, str, float], AsyncOpenAI] = {}


class FencedJsonOutputSchema(AgentOutputSchema):
    def validate_json(self, json_str: str) -> Any:
        lines = json_str.strip().splitlines()
        if lines and lines[0].strip().lower() in {"```", "```json"}:
            lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            json_str = "\n".join(lines).strip()
        return super().validate_json(json_str)


def build_run_config(
    settings: Settings,
    *,
    workflow_name: str,
    trace_metadata: dict[str, Any] | None = None,
    timeout_seconds: float | None = None,
) -> RunConfig:
    base_url = settings.llm_base_url or "https://api.openai.com/v1"
    timeout = float(timeout_seconds if timeout_seconds is not None else settings.llm_timeout_seconds)
    client, should_close = _client_for(settings, base_url=base_url, timeout_seconds=timeout)
    config = RunConfig(
        model_provider=OpenAIProvider(
            openai_client=client,
            use_responses=_use_responses_api(settings.llm_provider, base_url),
        ),
        tracing_disabled=True,
        trace_include_sensitive_data=False,
        workflow_name=workflow_name,
        trace_metadata=trace_metadata or {},
    )
    setattr(config, "_invoice_openai_client", client)
    setattr(config, "_invoice_close_openai_client", should_close)
    return config


def run_agent_sync(*args: Any, **kwargs: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run_agent_and_close(*args, **kwargs))
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(lambda: asyncio.run(_run_agent_and_close(*args, **kwargs))).result()


async def _run_agent_and_close(*args: Any, **kwargs: Any) -> Any:
    config = kwargs.get("run_config")
    try:
        return await Runner.run(*args, **kwargs)
    finally:
        await close_run_config_client(config)


async def close_run_config_client(config: Any) -> None:
    if not getattr(config, "_invoice_close_openai_client", True):
        return
    client = getattr(config, "_invoice_openai_client", None)
    if client is not None:
        await _safe_close_openai_client(client)


def enable_shared_openai_clients() -> None:
    global _SHARED_CLIENTS_ENABLED
    _SHARED_CLIENTS_ENABLED = True


async def close_shared_openai_clients() -> None:
    global _SHARED_CLIENTS_ENABLED
    clients = list(_SHARED_CLIENTS.values())
    _SHARED_CLIENTS.clear()
    _SHARED_CLIENTS_ENABLED = False
    for client in clients:
        await _safe_close_openai_client(client)


def _client_for(settings: Settings, *, base_url: str, timeout_seconds: float) -> tuple[AsyncOpenAI, bool]:
    if not _SHARED_CLIENTS_ENABLED:
        return (
            AsyncOpenAI(
                api_key=settings.llm_api_key,
                base_url=base_url,
                timeout=timeout_seconds,
                max_retries=0,
            ),
            True,
        )
    key = (settings.llm_api_key or "", base_url.rstrip("/"), timeout_seconds)
    client = _SHARED_CLIENTS.get(key)
    if client is None:
        client = AsyncOpenAI(
            api_key=settings.llm_api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=0,
        )
        _SHARED_CLIENTS[key] = client
    return client, False


def _use_responses_api(provider: str, base_url: str) -> bool:
    target = (str(provider or "").lower(), str(base_url or "").rstrip("/"))
    return target in {("openai", "https://api.openai.com/v1"), ("deepseek", "https://api.deepseek.com")}


async def _safe_close_openai_client(client: AsyncOpenAI) -> None:
    try:
        await client.close()
    except RuntimeError as exc:
        if "Event loop is closed" not in str(exc):
            raise
