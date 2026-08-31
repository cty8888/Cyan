"""模型 tool_call 参数 JSON 容错解析，以及流式分片累积。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cyan.errors import InvalidToolArgumentsError
from cyan.llm.parser import StreamAssembler, parse_tool_arguments


def test_plain_json():
    assert parse_tool_arguments('{"path": "a.py"}') == {"path": "a.py"}


def test_empty_arguments():
    assert parse_tool_arguments("") == {}


def test_strips_markdown_fence():
    assert parse_tool_arguments('```json\n{"a": 1}\n```') == {"a": 1}


def test_trailing_comma():
    assert parse_tool_arguments('{"a": 1,}') == {"a": 1}


def test_surrounding_text():
    assert parse_tool_arguments('好的 {"a": 1} 完成') == {"a": 1}


def test_invalid_json_raises():
    with pytest.raises(InvalidToolArgumentsError):
        parse_tool_arguments("{不是JSON", tool_name="read_file")


def _chunk(content=None, tool_calls=None, finish_reason=None, usage=None):
    """构造一个近似 OpenAI 兼容 SDK ``ChatCompletionChunk`` 的假分片。"""
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=usage)


def _tool_call_delta(index, *, call_id=None, name=None, arguments=None):
    function = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(index=index, id=call_id, function=function)


def test_stream_assembler_accumulates_text():
    assembler = StreamAssembler()
    assert assembler.feed(_chunk(content="Hello")) == "Hello"
    assert assembler.feed(_chunk(content=" world")) == " world"
    response = assembler.finalize()
    assert response.message.text == "Hello world"
    assert response.message.tool_calls == []


def test_stream_assembler_ignores_chunks_without_content():
    assembler = StreamAssembler()
    assert assembler.feed(_chunk()) == ""
    assert assembler.finalize().message.text is None


def test_stream_assembler_accumulates_tool_call_across_chunks():
    assembler = StreamAssembler()
    assembler.feed(
        _chunk(
            tool_calls=[
                _tool_call_delta(0, call_id="call_1", name="read_file", arguments='{"pat')
            ]
        )
    )
    assembler.feed(_chunk(tool_calls=[_tool_call_delta(0, arguments='h": "a.py"}')]))
    response = assembler.finalize()
    calls = response.message.tool_calls
    assert len(calls) == 1
    assert calls[0].id == "call_1"
    assert calls[0].name == "read_file"
    assert calls[0].arguments == '{"path": "a.py"}'


def test_stream_assembler_keeps_tool_call_order_by_index():
    assembler = StreamAssembler()
    assembler.feed(
        _chunk(
            tool_calls=[
                _tool_call_delta(1, call_id="call_b", name="write_file", arguments="{}"),
                _tool_call_delta(0, call_id="call_a", name="read_file", arguments="{}"),
            ]
        )
    )
    calls = assembler.finalize().message.tool_calls
    assert [c.id for c in calls] == ["call_a", "call_b"]


def test_stream_assembler_finish_reason_and_usage_from_last_chunk():
    assembler = StreamAssembler()
    assembler.feed(_chunk(content="hi"))
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    assembler.feed(_chunk(finish_reason="stop", usage=usage))
    response = assembler.finalize()
    assert response.finish_reason == "stop"
    assert response.usage.prompt_tokens == 10
    assert response.usage.completion_tokens == 5
    assert response.usage.total_tokens == 15


def test_stream_assembler_finalize_without_feed_is_empty_response():
    response = StreamAssembler().finalize()
    assert response.message.text is None
    assert response.message.tool_calls == []
    assert response.finish_reason == "stop"
