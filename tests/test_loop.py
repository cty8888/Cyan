"""Agent Loop：终止条件、审批与上下文完整性。"""

from __future__ import annotations

import json
import sys

from cyan.core.prompts import EMPTY_REPLY_CONTINUE_MSG, TRUNCATION_CONTINUE_MSG
from cyan.core.types import (
    ApprovalRequired,
    AssistantReply,
    AssistantReplyDelta,
    Notice,
    StopReason,
    TaskFinished,
    ToolCallDelta,
    ToolFinished,
    ToolStarted,
)
from cyan.llm.types import (
    AssistantMessage,
    ContinueMessage,
    FileBlock,
    LLMResponse,
    ToolCallBlock,
    ToolMessage,
    UserMessage,
    Usage,
)
from cyan.security.types import ApprovalDecision, PermissionMode
from cyan.session import Session
from cyan.settings import CliSettings, LoopLimits

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


def test_final_reply_streams_delta_before_full_text(env):
    """FakeLLM 走 LLMClient 的默认 chat_stream 兜底：一个 delta 分片 + 一条完整 AssistantReply。"""
    llm = FakeLLM([AssistantMessage.of("已完成任务。")])
    runtime = make_runtime(env, llm)
    events, reason = drive(runtime, "随便做点什么")
    assert reason is StopReason.COMPLETED

    deltas = [e for e in events if isinstance(e, AssistantReplyDelta)]
    replies = [e for e in events if isinstance(e, AssistantReply)]
    assert deltas == [AssistantReplyDelta(text="已完成任务。")]
    assert replies == [AssistantReply(text="已完成任务。")]
    assert events.index(deltas[0]) < events.index(replies[0])


def test_tool_call_streams_as_delta_before_tool_started(env, tmp_path):
    """FakeLLM 走兜底 chat_stream：tool_call 的完整参数会先当一次分片以 ToolCallDelta 发出，再触发 ToolStarted。"""
    llm = FakeLLM([
        tool_call("write_file", '{"path": "a.py", "content": "x = 1"}', "c1"),
        AssistantMessage.of("完成。"),
    ])
    runtime = make_runtime(env, llm)
    events, reason = drive(runtime, "写个文件")
    assert reason is StopReason.COMPLETED

    deltas = [e for e in events if isinstance(e, ToolCallDelta)]
    started = [e for e in events if isinstance(e, ToolStarted)]
    assert len(deltas) == 1
    assert deltas[0].index == 0
    assert deltas[0].call_id == "c1"
    assert deltas[0].name == "write_file"
    assert deltas[0].arguments_delta == '{"path": "a.py", "content": "x = 1"}'
    assert len(started) == 1
    assert events.index(deltas[0]) < events.index(started[0])


def test_stops_at_max_iterations(make_env):
    env = make_env(loop=LoopLimits(max_iterations=3))
    llm = FakeLLM([tool_call("list_dir", '{"path": ".", "depth": %d}' % (i + 1), f"c{i}") for i in range(10)])
    runtime = make_runtime(env, llm)
    _, reason = drive(runtime, "循环")
    assert reason is StopReason.MAX_ITERATIONS


def test_runtime_loop_limits_edit_affects_termination_not_settings(env):
    """会话中途改 ``runtime.loop_limits``（比如 /loop 命令）要立刻生效；
    ``settings.loop`` 是拷贝源头，不应被这次修改动到。
    """
    llm = FakeLLM([tool_call("list_dir", '{"path": ".", "depth": %d}' % (i + 1), f"c{i}") for i in range(10)])
    runtime = make_runtime(env, llm)
    original_settings_max = runtime.settings.loop.max_iterations
    runtime.loop_limits.max_iterations = 2

    _, reason = drive(runtime, "循环")

    assert reason is StopReason.MAX_ITERATIONS
    assert runtime.settings.loop.max_iterations == original_settings_max


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


def test_consecutive_denials_do_not_count_as_failures(make_env):
    env = make_env(loop=LoopLimits(max_consecutive_tool_failures=3, max_iterations=20))
    llm = FakeLLM(
        [
            tool_call("write_file", '{"path": "deny%d.py", "content": "x"}' % i, f"d{i}")
            for i in range(4)
        ]
        + [AssistantMessage.of("改方案，不再写这些文件。")]
    )
    runtime = make_runtime(env, llm)
    _, reason = drive(runtime, "连拒", decision=ApprovalDecision.DENY)
    assert reason is StopReason.COMPLETED
    assert runtime.session.consecutive_tool_failures == 0


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
        isinstance(m, ContinueMessage) and m.text == TRUNCATION_CONTINUE_MSG
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
    assert names == {"list_dir", "read_file", "bash", "glob", "grep", "memory_list", "memory_read"}


def test_repeated_denials_of_same_write_trip_repeats(make_env):
    env = make_env(loop=LoopLimits(max_repeated_calls=3, max_iterations=10))
    llm = FakeLLM(
        [tool_call("write_file", '{"path": "same.py", "content": "x"}', f"d{i}") for i in range(4)]
        + [AssistantMessage.of("不再重试。")]
    )
    runtime = make_runtime(env, llm)
    _, reason = drive(runtime, "连拒同一文件", decision=ApprovalDecision.DENY)
    assert reason is StopReason.REPEATED_CALLS


def test_same_batch_bash_cwd_is_sequential(env, tmp_path):
    """同批两条 bash 按顺序执行，第一条 cd 会影响第二条。"""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "here.txt").write_text("nested\n", encoding="utf-8")
    calls = [
        ToolCallBlock(id="b1", name="bash", arguments='{"command": "cd pkg && pwd"}'),
        ToolCallBlock(id="b2", name="bash", arguments='{"command": "cat here.txt"}'),
    ]
    runtime = make_runtime(
        env,
        FakeLLM([AssistantMessage.of(tool_calls=calls), AssistantMessage.of("好了")]),
    )
    _, reason = drive(runtime, "顺序两条")
    assert reason is StopReason.COMPLETED
    second = runtime.session.tool_history.get("b2")
    assert second is not None and second.result is not None
    assert "nested" in (second.result.content or "")


def test_empty_reply_continues_instead_of_completing(env):
    llm = FakeLLM(
        [
            LLMResponse(message=AssistantMessage.of(""), finish_reason="stop", usage=Usage(10, 0, 10)),
            AssistantMessage.of("这回有内容了。"),
        ]
    )
    runtime = make_runtime(env, llm)
    events, reason = drive(runtime, "继续")
    assert reason is StopReason.COMPLETED
    notices = [e.message for e in events if isinstance(e, Notice)]
    assert any("没有给出回复" in m for m in notices)
    assert any(
        isinstance(m, ContinueMessage) and m.text == EMPTY_REPLY_CONTINUE_MSG
        for m in runtime.session.messages
    )


def test_run_with_file_refs_attaches_file_blocks_to_user_message(env):
    """``file_refs`` 要跟任务文本一起挂进本轮 ``UserMessage``，供模型与后续会话使用。"""
    llm = FakeLLM([AssistantMessage.of("看过了。")])
    runtime = make_runtime(env, llm)
    refs = [FileBlock(path="a.py", content="x = 1")]

    events, reason = drive(runtime, "看看 @a.py", file_refs=refs)

    assert reason is StopReason.COMPLETED
    user_messages = [m for m in runtime.session.messages if isinstance(m, UserMessage)]
    assert len(user_messages) == 1
    assert [b.path for b in user_messages[0].file_blocks] == ["a.py"]
    assert user_messages[0].to_api()["content"] == "看看 @a.py\n\n[文件 a.py]\n```\nx = 1\n```"


def test_run_without_file_refs_still_works(env):
    """不传 ``file_refs``（默认 None）时行为跟原来一样，只有 TextBlock。"""
    llm = FakeLLM([AssistantMessage.of("完成。")])
    runtime = make_runtime(env, llm)

    events, reason = drive(runtime, "随便做点什么")

    assert reason is StopReason.COMPLETED
    user_messages = [m for m in runtime.session.messages if isinstance(m, UserMessage)]
    assert user_messages[0].file_blocks == []


def test_loop_stores_validated_tool_arguments(env, tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    llm = FakeLLM(
        [
            tool_call("read_file", '{"path": "a.py",}', "c1"),
            AssistantMessage.of("读完了。"),
        ]
    )
    runtime = make_runtime(env, llm)
    _, reason = drive(runtime, "读文件")
    assert reason is StopReason.COMPLETED
    execution = runtime.session.tool_history.get("c1")
    assert execution is not None
    parsed = json.loads(execution.arguments)
    assert parsed["path"] == "a.py"
