from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from agents import RunConfig, Runner
from agents.models.openai_provider import OpenAIProvider
from openai import AsyncOpenAI

from app.config import Settings


def build_run_config(
    settings: Settings,
    *,
    workflow_name: str,
    trace_metadata: dict[str, Any] | None = None,
    timeout_seconds: float | None = None,
) -> RunConfig:
    base_url = settings.llm_base_url or "https://api.openai.com/v1"
    client = AsyncOpenAI(
        api_key=settings.llm_api_key,
        base_url=base_url,
        timeout=timeout_seconds if timeout_seconds is not None else settings.llm_timeout_seconds,
        max_retries=0,
    )
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
        client = getattr(config, "_invoice_openai_client", None)
        if client is not None:
            await client.close()


def _use_responses_api(provider: str, base_url: str) -> bool:
    if str(provider or "").lower() != "openai":
        return False
    return str(base_url or "").rstrip("/") == "https://api.openai.com/v1"
