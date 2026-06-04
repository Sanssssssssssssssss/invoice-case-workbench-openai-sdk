from __future__ import annotations

from types import SimpleNamespace

from app.llm import LlmClient


def test_streaming_completion_collects_reasoning_content(monkeypatch) -> None:
    client = LlmClient()
    recorded: list[dict[str, object]] = []
    client.set_thinking_recorder(recorded.append)
    content, reasoning, chunks, usage, finish_reason = client._complete_streaming(
        client=_fake_openai_client(
            [
                _chunk(reasoning="先检查金额。"),
                _chunk(reasoning="再核对税率。"),
                _chunk(content='{"ok": true}', finish_reason="stop"),
            ]
        ),
        kwargs={"model": "kimi-k2.5", "messages": []},
        role="evidence_reviewer",
        model="kimi-k2.5",
        prompt_version="test",
    )

    assert content == '{"ok": true}'
    assert reasoning == "先检查金额。再核对税率。"
    assert chunks == 2
    assert usage == {}
    assert finish_reason == "stop"
    assert recorded[-1]["status"] == "completed"
    assert recorded[-1]["reasoning_chunks"] == 2


def test_streaming_completion_allows_content_without_reasoning() -> None:
    client = LlmClient()
    recorded: list[dict[str, object]] = []
    client.set_thinking_recorder(recorded.append)
    content, reasoning, chunks, _usage, _finish_reason = client._complete_streaming(
        client=_fake_openai_client([_chunk(content='{"ok": true}', finish_reason="stop")]),
        kwargs={"model": "gpt-test", "messages": []},
        role="planner",
        model="gpt-test",
        prompt_version="test",
    )

    assert content == '{"ok": true}'
    assert reasoning == ""
    assert chunks == 0
    assert recorded == []


def _fake_openai_client(chunks: list[object]) -> object:
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_kwargs: iter(chunks))))


def _chunk(*, reasoning: str = "", content: str = "", finish_reason: str = "") -> object:
    delta = SimpleNamespace(content=content)
    if reasoning:
        setattr(delta, "reasoning_content", reasoning)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)])
