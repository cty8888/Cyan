"""Agent Loop：终止条件、审批与上下文完整性。"""

from __future__ import annotations

import json
import sys

from coding_agent.core.types import ApprovalRequired, StopReason, ToolFinished
from coding_agent.llm.types import AssistantMessage, ToolMessage
from coding_agent.security.types import ApprovalDecision, PermissionMode
from coding_agent.session import Session
from coding_agent.settings import CliSettings, LoopLimits

from .conftest import FakeLLM, drive, make_runtime, tool_call


def test_completes_write_then_bash(env, tmp_path):
    run_loop = json.dumps({"command": f"{sys.executable} loop.py"})
    llm = FakeLLM([
        tool_call("write_file", '{"path": "loop.py", "content": "print(41+1)"}'),
        tool_call("bash", run_loop, "c2"),
        AssistantMessage.of("已创建并验证 loop.py，输出 42。"),
    ])
    runtime = make_runtime(env, llm)
    events, reason = drive(runtime, "写个脚本")
    assert reason is StopReason.COMPLETED
    assert sum(isinstance(e, ToolFinished) for e in events) == 2
    assert (tmp_path / "loop.py").is_file()


def test_stops_at_max_iterations(make_env):
    env = make_env(loop=LoopLimits(max_iterations=3))
    llm = FakeLLM([tool_call("list_dir", '{"path": ".", "depth": %d}' % (i + 1), f"c{i}") for i in range(10)])
    runtime = make_runtime(env, llm)
    _, reason = drive(runtime, "循环")
    assert reason is StopReason.MAX_ITERATIONS


def test_stops_on_repeated_calls(env):
    llm = FakeLLM([tool_call("read_file", '{"path": "nope.py"}', f"r{i}") for i in range(10)])
    runtime = make_runtime(env, llm)
    _, reason = drive(runtime, "重复")
    assert reason is StopReason.REPEATED_CALLS


def test_stops_on_consecutive_failures(env):
    llm = FakeLLM([tool_call("read_file", '{"path": "miss%d.py"}' % i, f"f{i}") for i in range(10)])
    runtime = make_runtime(env, llm)
    _, reason = drive(runtime, "连续失败")
    assert reason is StopReason.TOOL_FAILURES


def test_consecutive_denials_count_as_failures(make_env):
    env = make_env(loop=LoopLimits(max_consecutive_tool_failures=3, max_iterations=20))
    llm = FakeLLM([
        tool_call("write_file", '{"path": "deny%d.py", "content": "x"}' % i, f"d{i}")
        for i in range(10)
    ])
    runtime = make_runtime(env, llm)
    _, reason = drive(runtime, "连拒", decision=ApprovalDecision.DENY)
    assert reason is StopReason.TOOL_FAILURES


def test_invalid_arguments_feed_tool_message(env):
    llm = FakeLLM([
        tool_call("read_file", "{不是JSON"),
        AssistantMessage.of("参数写错了，已放弃。"),
    ])
    runtime = make_runtime(env, llm)
    _, reason = drive(runtime, "坏参数")
    assert reason is StopReason.COMPLETED
    tool_msgs = [m for m in runtime.session.messages if isinstance(m, ToolMessage)]
    block = tool_msgs[0].tool_result
    execution = runtime.session.tool_history.get(block.tool_call_id) if block else None
    assert len(tool_msgs) == 1
    assert execution is not None
    assert "不是合法 JSON" in (execution.result.content or "")


def test_denying_exec_does_not_run_command(env, tmp_path):
    llm = FakeLLM([
        tool_call("bash", '{"command": "touch denied_by_bash.txt"}'),
        AssistantMessage.of("好的，已放弃执行。"),
    ])
    runtime = make_runtime(env, llm)
    events, _ = drive(runtime, "touch", decision=ApprovalDecision.DENY)
    assert any(isinstance(e, ApprovalRequired) for e in events)
    assert not (tmp_path / "denied_by_bash.txt").exists()


def test_write_approval_includes_diff(env):
    llm = FakeLLM([
        tool_call("write_file", '{"path": ".env", "content": "K=1"}'),
        AssistantMessage.of("好的。"),
    ])
    runtime = make_runtime(env, llm)
    events, _ = drive(runtime, "写 env", decision=ApprovalDecision.ALLOW_ONCE)
    approval = next(e.request for e in events if isinstance(e, ApprovalRequired))
    assert approval.detail_format == "diff"
    assert "+K=1" in (approval.detail or "")


def test_readonly_tool_skips_approval(env):
    llm = FakeLLM([tool_call("list_dir", '{"path": "."}'), AssistantMessage.of("看完了")])
    runtime = make_runtime(env, llm)
    events, _ = drive(runtime, "看目录")
    assert not any(isinstance(e, ApprovalRequired) for e in events)


def test_every_tool_call_has_tool_message(env):
    llm = FakeLLM([
        tool_call("write_file", '{"path": ".env", "content": "K=1"}'),
        AssistantMessage.of("好"),
    ])
    runtime = make_runtime(env, llm)
    drive(runtime, "改 env", decision=ApprovalDecision.DENY)
    assistant_calls = sum(
        len(m.tool_calls) for m in runtime.session.messages if isinstance(m, AssistantMessage)
    )
    tool_replies = sum(1 for m in runtime.session.messages if isinstance(m, ToolMessage))
    assert assistant_calls == tool_replies


def test_plan_mode_exposes_read_and_bash(make_env, tmp_path):
    env = make_env(cli=CliSettings(permission_mode=PermissionMode.PLAN))
    session = Session.create(workspace=tmp_path, system_prompt="", permission_mode=PermissionMode.PLAN)
    runtime = make_runtime(env, FakeLLM([AssistantMessage.of("ok")]), session)
    names = {s["function"]["name"] for s in runtime.schemas_for_mode()}
    assert names == {"list_dir", "read_file", "bash"}
