"""Agent Loop：终止条件、审批与上下文完整性。"""

from __future__ import annotations

import json
import sys

from coding_agent.core.prompts import TRUNCATION_CONTINUE_MSG
from coding_agent.core.types import ApprovalRequired, AssistantReply, Notice, StopReason, TaskFinished, ToolFinished
from coding_agent.llm.types import AssistantMessage, LLMResponse, ToolCallBlock, ToolMessage, Usage, UserMessage
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


def _assert_tool_calls_paired(session: Session) -> None:
    assistant_calls = sum(
        len(m.tool_calls) for m in session.messages if isinstance(m, AssistantMessage)
    )
    tool_replies = sum(1 for m in session.messages if isinstance(m, ToolMessage))
    assert assistant_calls == tool_replies


def test_early_stop_pairs_remaining_tool_calls(make_env):
    env = make_env(loop=LoopLimits(max_consecutive_tool_failures=3, max_iterations=5))
    calls = [
        ToolCallBlock(id=f"f{i}", name="read_file", arguments='{"path": "miss%d.py"}' % i)
        for i in range(5)
    ]
    runtime = make_runtime(env, FakeLLM([AssistantMessage.of(tool_calls=calls)]))
    _, reason = drive(runtime, "一批失败")
    assert reason is StopReason.TOOL_FAILURES
    _assert_tool_calls_paired(runtime.session)
    leftover = runtime.session.tool_history.get("f3")
    assert leftover is not None
    assert leftover.result is not None
    assert "任务已终止" in (leftover.result.content or "")


def test_interrupt_after_assistant_reply_pairs_tool_calls(env):
    """assistant 已入会话、工具还没跑时中断，仍要补齐 tool 回复。"""
    calls = [
        ToolCallBlock(id="c1", name="read_file", arguments='{"path": "a.py"}'),
        ToolCallBlock(id="c2", name="list_dir", arguments='{"path": "."}'),
    ]
    runtime = make_runtime(
        env,
        FakeLLM([AssistantMessage.of("我先读文件。", tool_calls=calls)]),
    )
    stream = runtime.run("读一下")
    reply = None
    while True:
        event = stream.send(reply)
        reply = None
        if isinstance(event, AssistantReply):
            try:
                thrown = stream.throw(KeyboardInterrupt())
            except (StopIteration, KeyboardInterrupt):
                thrown = None
            if isinstance(thrown, TaskFinished):
                assert thrown.reason is StopReason.USER_ABORT
                stream.close()
            break
    _assert_tool_calls_paired(runtime.session)
    leftover = runtime.session.tool_history.get("c1")
    assert leftover is not None and leftover.result is not None
    assert "用户中断" in (leftover.result.content or "")


def test_repeated_calls_pair_remaining_in_batch(env):
    calls = [
        ToolCallBlock(id=f"r{i}", name="read_file", arguments='{"path": "nope.py"}')
        for i in range(3)
    ]
    calls.append(ToolCallBlock(id="extra", name="list_dir", arguments='{"path": "."}'))
    runtime = make_runtime(env, FakeLLM([AssistantMessage.of(tool_calls=calls)]))
    _, reason = drive(runtime, "重复一批")
    assert reason is StopReason.REPEATED_CALLS
    _assert_tool_calls_paired(runtime.session)
    extra = runtime.session.tool_history.get("extra")
    assert extra is not None
    assert extra.result is not None
    assert "任务已终止" in (extra.result.content or "")


def test_successful_reread_does_not_count_as_repeat(env, tmp_path):
    (tmp_path / "a.py").write_text("print(1)\n", encoding="utf-8")
    llm = FakeLLM([tool_call("read_file", '{"path": "a.py"}', f"r{i}") for i in range(5)])
    runtime = make_runtime(env, llm)
    _, reason = drive(runtime, "反复读")
    assert reason is StopReason.COMPLETED


def test_alternating_failed_reads_are_not_repeated_calls(make_env):
    env = make_env(loop=LoopLimits(max_repeated_calls=3, max_consecutive_tool_failures=20, max_iterations=10))
    llm = FakeLLM(
        [
            tool_call("read_file", '{"path": "miss_a.py"}', "a1"),
            tool_call("read_file", '{"path": "miss_b.py"}', "b1"),
            tool_call("read_file", '{"path": "miss_a.py"}', "a2"),
            tool_call("read_file", '{"path": "miss_b.py"}', "b2"),
            tool_call("read_file", '{"path": "miss_a.py"}', "a3"),
            AssistantMessage.of("换方案"),
        ]
    )
    runtime = make_runtime(env, llm)
    _, reason = drive(runtime, "交替失败")
    assert reason is StopReason.COMPLETED


def test_failing_commands_do_not_trip_tool_failures(make_env):
    env = make_env(loop=LoopLimits(max_consecutive_tool_failures=3, max_iterations=10))
    llm = FakeLLM(
        [
            tool_call("bash", '{"command": "exit 1"}', "b1"),
            tool_call("bash", '{"command": "exit 2"}', "b2"),
            tool_call("bash", '{"command": "exit 3"}', "b3"),
            AssistantMessage.of("测试红了，接着改。"),
        ]
    )
    runtime = make_runtime(env, llm)
    _, reason = drive(runtime, "跑测试")
    assert reason is StopReason.COMPLETED
    assert runtime.session.consecutive_tool_failures == 0


def test_truncated_reply_continues_instead_of_completing(env):
    llm = FakeLLM(
        [
            LLMResponse(
                message=AssistantMessage.of("只写了一半"),
                finish_reason="length",
                usage=Usage(10, 5, 15),
            ),
            AssistantMessage.of("从断点补完了。"),
        ]
    )
    runtime = make_runtime(env, llm)
    events, reason = drive(runtime, "写长文")
    assert reason is StopReason.COMPLETED
    notices = [e.message for e in events if isinstance(e, Notice)]
    assert any("被截断" in m for m in notices)
    assert any(
        isinstance(m, UserMessage) and m.text == TRUNCATION_CONTINUE_MSG
        for m in runtime.session.messages
    )
    assert llm.script == []


def test_repeated_truncation_stops_task(make_env):
    env = make_env(loop=LoopLimits(max_consecutive_tool_failures=3, max_iterations=10))
    llm = FakeLLM(
        [
            LLMResponse(
                message=AssistantMessage.of(f"截断{i}"),
                finish_reason="length",
                usage=Usage(10, 5, 15),
            )
            for i in range(5)
        ]
    )
    runtime = make_runtime(env, llm)
    events, reason = drive(runtime, "写长文")
    assert reason is StopReason.MAX_ITERATIONS
    notices = [e.message for e in events if isinstance(e, Notice)]
    assert any("连续" in m and "截断" in m for m in notices)


def test_truncated_reply_with_tool_calls_still_runs(env, tmp_path):
    (tmp_path / "a.py").write_text("print(1)\n", encoding="utf-8")
    llm = FakeLLM(
        [
            LLMResponse(
                message=tool_call("read_file", '{"path": "a.py"}'),
                finish_reason="length",
                usage=Usage(10, 5, 15),
            ),
            AssistantMessage.of("读完了"),
        ]
    )
    runtime = make_runtime(env, llm)
    _, reason = drive(runtime, "读文件")
    assert reason is StopReason.COMPLETED
    assert runtime.session.tool_history.get("c1") is not None


def test_plan_mode_exposes_read_and_bash(make_env, tmp_path):
    env = make_env(cli=CliSettings(permission_mode=PermissionMode.PLAN))
    session = Session.create(workspace=tmp_path, system_prompt="", permission_mode=PermissionMode.PLAN)
    runtime = make_runtime(env, FakeLLM([AssistantMessage.of("ok")]), session)
    names = {s["function"]["name"] for s in runtime.schemas_for_mode()}
    assert names == {"list_dir", "read_file", "bash"}
