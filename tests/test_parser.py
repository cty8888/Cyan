"""模型 tool_call 参数 JSON 容错解析。"""

from __future__ import annotations

import pytest

from coding_agent.errors import InvalidToolArgumentsError
from coding_agent.llm.parser import parse_tool_arguments


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
