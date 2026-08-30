"""组窗：全部 messages 送出；工具正文过长只在 wire 上截尾，Session 不改。"""

from __future__ import annotations

from cyan.context.builder import ContextBuilder
from cyan.context.types import ContextPolicy
from cyan.llm.types import AssistantMessage, ToolCallBlock, ToolMessage, UserMessage
from cyan.session import Session
from cyan.settings.tools import DEFAULT_TOOL_RESULT_CHARS, ToolLimits

from .conftest import FakeLLM, make_runtime


def _session_with_tool(tmp_path, content: str) -> Session:
    session = Session.create(workspace=tmp_path, system_prompt="sys")
    session.add(UserMessage.of("读文件"))
    session.add(
        AssistantMessage.of(tool_calls=[ToolCallBlock(id="c1", name="read_file", arguments="{}")])
    )
    session.start_tool_execution(call_id="c1", tool_name="read_file", arguments="{}")
    session.finish_tool_execution(call_id="c1", ok=True, content=content)
    session.add(ToolMessage.of("c1"))
    return session


def test_short_tool_result_unchanged(tmp_path):
    session = _session_with_tool(tmp_path, "hello")
    payloads = ContextBuilder.from_policy(ContextPolicy()).build_messages(
        session.messages, session.tool_history
    )
    tool = next(p for p in payloads if p["role"] == "tool")
    assert tool["content"] == "hello"


def test_long_tool_result_truncated_in_wire_only(tmp_path):
    raw = "x" * 50
    session = _session_with_tool(tmp_path, raw)
    builder = ContextBuilder.from_policy(ContextPolicy(max_tool_result_chars=20))
    payloads = builder.build_messages(session.messages, session.tool_history)
    tool = next(p for p in payloads if p["role"] == "tool")
    assert tool["content"] == "x" * 6 + "...[truncated]"
    assert len(tool["content"]) == 20
    assert session.tool_history.get("c1").result.content == raw


def test_read_budget_does_not_exceed_context_truncation():
    assert ToolLimits().max_file_read_chars <= ContextPolicy().max_tool_result_chars
    assert ToolLimits().max_file_read_chars == DEFAULT_TOOL_RESULT_CHARS
    assert ContextPolicy().max_tool_result_chars == DEFAULT_TOOL_RESULT_CHARS


def test_zero_limit_does_not_truncate(tmp_path):
    raw = "y" * 50
    session = _session_with_tool(tmp_path, raw)
    builder = ContextBuilder.from_policy(ContextPolicy(max_tool_result_chars=0))
    payloads = builder.build_messages(session.messages, session.tool_history)
    tool = next(p for p in payloads if p["role"] == "tool")
    assert tool["content"] == raw
    assert session.tool_history.get("c1").result.content == raw


def test_runtime_context_policy_follows_tool_limits(make_env):
    env = make_env(tools=ToolLimits(max_file_read_chars=1234))
    runtime = make_runtime(env, FakeLLM([]))
    assert runtime.context_policy.max_tool_result_chars == 1234
    assert runtime.context_builder.max_tool_result_chars == 1234
