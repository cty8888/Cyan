"""DeepSeekClient.chat_stream：SSE 分片消费、重试语义、stream=False 退化。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cyan.errors import LLMConnectionError
from cyan.llm.deepseek import DeepSeekClient
from cyan.settings.llm import LLMSettings


def _chunk(content=None, finish_reason=None, usage=None, tool_calls=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=usage)


def _tool_call_delta(index, *, call_id=None, name=None, arguments=None):
    function = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(index=index, id=call_id, function=function)


def _client(**settings_kwargs) -> DeepSeekClient:
    client = DeepSeekClient(LLMSettings(api_key="k", max_retries=2, **settings_kwargs))
    return client


def _patch_create(client: DeepSeekClient, fake_create) -> None:
    client._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
    )


def test_chat_stream_yields_deltas_and_returns_final_response():
    client = _client()
    usage = SimpleNamespace(prompt_tokens=3, completion_tokens=2, total_tokens=5)

    def fake_create(**kwargs):
        assert kwargs["stream"] is True
        assert kwargs["stream_options"] == {"include_usage": True}
        return iter(
            [
                _chunk(content="Hello"),
                _chunk(content=" world"),
                _chunk(finish_reason="stop", usage=usage),
            ]
        )

    _patch_create(client, fake_create)

    gen = client.chat_stream([{"role": "user", "content": "hi"}])
    deltas = []
    try:
        while True:
            deltas.append(next(gen).text_delta)
    except StopIteration as stop:
        response = stop.value

    assert deltas == ["Hello", " world"]
    assert response.message.text == "Hello world"
    assert response.finish_reason == "stop"
    assert response.usage.total_tokens == 5


def test_chat_stream_retries_before_first_chunk():
    client = _client()
    attempts = {"count": 0}

    def fake_create(**kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise ConnectionError("boom")
        return iter([_chunk(content="ok"), _chunk(finish_reason="stop")])

    _patch_create(client, fake_create)

    gen = client.chat_stream([{"role": "user", "content": "hi"}])
    deltas = []
    try:
        while True:
            deltas.append(next(gen).text_delta)
    except StopIteration as stop:
        response = stop.value

    assert attempts["count"] == 2
    assert deltas == ["ok"]
    assert response.message.text == "ok"


def test_chat_stream_does_not_retry_after_emitting_content():
    """已经吐出内容给用户看之后，中途报错不重试，直接把错误抛出去。"""
    client = _client()
    attempts = {"count": 0}

    def fake_create(**kwargs):
        attempts["count"] += 1

        def _raise_after_first():
            yield _chunk(content="partial")
            raise ConnectionError("dropped mid-stream")

        return _raise_after_first()

    _patch_create(client, fake_create)

    gen = client.chat_stream([{"role": "user", "content": "hi"}])
    assert next(gen).text_delta == "partial"
    with pytest.raises(LLMConnectionError):
        next(gen)
    assert attempts["count"] == 1


def test_chat_stream_falls_back_to_chat_when_stream_disabled():
    client = _client(stream=False)

    def fake_create(**kwargs):
        assert "stream" not in kwargs
        message = SimpleNamespace(content="done", tool_calls=None)
        choice = SimpleNamespace(message=message, finish_reason="stop")
        usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2)
        return SimpleNamespace(choices=[choice], usage=usage)

    _patch_create(client, fake_create)

    gen = client.chat_stream([{"role": "user", "content": "hi"}])
    chunks = []
    try:
        while True:
            chunks.append(next(gen))
    except StopIteration as stop:
        response = stop.value

    assert [c.text_delta for c in chunks] == ["done"]
    assert response.message.text == "done"


def test_chat_stream_yields_tool_call_argument_deltas():
    client = _client()

    def fake_create(**kwargs):
        return iter(
            [
                _chunk(
                    tool_calls=[
                        _tool_call_delta(0, call_id="call_1", name="write_file", arguments='{"path": "a.py", "content": "')
                    ]
                ),
                _chunk(tool_calls=[_tool_call_delta(0, arguments='print(1)"}')]),
                _chunk(finish_reason="tool_calls"),
            ]
        )

    _patch_create(client, fake_create)

    gen = client.chat_stream([{"role": "user", "content": "hi"}])
    chunks = []
    try:
        while True:
            chunks.append(next(gen))
    except StopIteration as stop:
        response = stop.value

    assert [c.tool_call_arguments_delta for c in chunks] == [
        '{"path": "a.py", "content": "',
        'print(1)"}',
    ]
    assert chunks[0].tool_call_name == "write_file"
    assert chunks[0].tool_call_id == "call_1"
    assert chunks[1].tool_call_name is None
    calls = response.message.tool_calls
    assert len(calls) == 1
    assert calls[0].name == "write_file"
