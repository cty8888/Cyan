"""会话压缩：被压缩段换成 SummaryMessage，对应 tool_history 先喂后删。"""

from __future__ import annotations

from io import StringIO

from rich.console import Console

from cyan.cli.commands import build_default_commands
from cyan.cli.renderer import Renderer
from cyan.settings.tools import DEFAULT_TOOL_RESULT_CHARS
from cyan.session.compact import (
    CompactPolicy,
    estimate_payload_tokens,
    find_keep_from,
    needs_compact,
    resolve_keep_from,
    try_compact,
)
from cyan.core.prompts import COMPACT_SYSTEM_PROMPT, TRUNCATION_CONTINUE_MSG
from cyan.core.types import Notice, StopReason
from cyan.errors import LLMContextOverflowError, LLMError
from cyan.llm.types import (
    AssistantMessage,
    ContinueMessage,
    LLMResponse,
    SummaryMessage,
    SystemMessage,
    ToolCallBlock,
    ToolMessage,
    Usage,
    UserMessage,
)
from cyan.session import Session

from .conftest import FakeLLM, drive, make_runtime, tool_call


def _add_turn(session: Session, user_text: str, call_id: str, tool_output: str) -> None:
    session.add(UserMessage.of(user_text))
    session.add(
        AssistantMessage.of(
            tool_calls=[ToolCallBlock(id=call_id, name="read_file", arguments='{"path": "a.py"}')]
        )
    )
    session.start_tool_execution(call_id=call_id, tool_name="read_file", arguments='{"path": "a.py"}')
    session.finish_tool_execution(call_id=call_id, ok=True, content=tool_output)
    session.add(ToolMessage.of(call_id))


def _three_turns(tmp_path) -> Session:
    session = Session.create(workspace=tmp_path, system_prompt="sys")
    _add_turn(session, "任务A", "ca", "output-A-unique")
    _add_turn(session, "任务B", "cb", "output-B-unique")
    _add_turn(session, "任务C", "cc", "output-C-unique")
    return session


def _add_assistant_round(session: Session, call_id: str, tool_output: str) -> None:
    session.add(
        AssistantMessage.of(
            tool_calls=[ToolCallBlock(id=call_id, name="read_file", arguments='{"path": "a.py"}')]
        )
    )
    session.start_tool_execution(call_id=call_id, tool_name="read_file", arguments='{"path": "a.py"}')
    session.finish_tool_execution(call_id=call_id, ok=True, content=tool_output)
    session.add(ToolMessage.of(call_id))


def test_find_keep_from_skips_summary_and_cuts_on_user(tmp_path):
    session = Session.create(workspace=tmp_path, system_prompt="sys")
    session.add(SummaryMessage.of("旧摘要"))
    _add_turn(session, "任务A", "ca", "a")
    _add_turn(session, "任务B", "cb", "b")
    _add_turn(session, "任务C", "cc", "c")
    keep_from = find_keep_from(session.messages, keep_recent_turns=2)
    assert keep_from is not None
    assert isinstance(session.messages[keep_from], UserMessage)
    assert session.messages[keep_from].text == "任务B"


def test_find_keep_from_cuts_early_rounds_in_one_task(tmp_path):
    session = Session.create(workspace=tmp_path, system_prompt="sys")
    session.add(UserMessage.of("做任务"))
    _add_assistant_round(session, "c0", "round-0")
    _add_assistant_round(session, "c1", "round-1")
    _add_assistant_round(session, "c2", "round-2")
    keep_from = find_keep_from(session.messages, keep_recent_turns=2)
    assert keep_from is not None
    assert isinstance(session.messages[keep_from], AssistantMessage)
    assert session.messages[keep_from].tool_calls[0].id == "c1"


def test_preferred_cut_requires_more_than_keep_turns(tmp_path):
    session = Session.create(workspace=tmp_path, system_prompt="sys")
    _add_turn(session, "任务A", "ca", "a")
    _add_turn(session, "任务B", "cb", "b")
    assert find_keep_from(session.messages, keep_recent_turns=2) is None
    assert resolve_keep_from(session.messages, keep_recent_turns=2) is not None


def test_fallback_compacts_two_assistant_rounds(tmp_path):
    session = Session.create(workspace=tmp_path, system_prompt="sys")
    session.add(UserMessage.of("做任务"))
    _add_assistant_round(session, "c0", "round-0-unique")
    _add_assistant_round(session, "c1", "round-1-unique")

    def call_llm(messages, tools=None):
        return LLMResponse(message=AssistantMessage.of("两轮摘要"), usage=Usage(20, 5, 25))

    assert try_compact(session, call_llm, CompactPolicy()) is True
    assert isinstance(session.messages[1], SummaryMessage)
    assert session.tool_history.get("c0") is None
    assert session.tool_history.get("c1") is not None
    users = [m for m in session.messages if isinstance(m, UserMessage) and not isinstance(m, SummaryMessage)]
    assert [m.text for m in users] == ["做任务"]


def test_emergency_cut_drops_all_assistant_rounds(tmp_path):
    session = Session.create(workspace=tmp_path, system_prompt="sys")
    session.add(UserMessage.of("做任务"))
    _add_assistant_round(session, "c0", "only-round")
    keep_from = find_keep_from(session.messages, keep_recent_turns=0)
    assert keep_from == len(session.messages)

    def call_llm(messages, tools=None):
        return LLMResponse(message=AssistantMessage.of("紧急摘要"), usage=Usage(10, 4, 14))

    assert try_compact(session, call_llm, CompactPolicy(), max_keep=0) is True
    assert isinstance(session.messages[1], SummaryMessage)
    assert session.messages[1].text == "紧急摘要"
    assert session.tool_history.get("c0") is None
    users = [m for m in session.messages if isinstance(m, UserMessage) and not isinstance(m, SummaryMessage)]
    assert [m.text for m in users] == ["做任务"]
    assert not any(isinstance(m, AssistantMessage) for m in session.messages)


def test_emergency_cut_without_assistant_when_user_is_huge(tmp_path):
    session = Session.create(workspace=tmp_path, system_prompt="sys")
    huge = "贴一段代码\n" + ("x" * (DEFAULT_TOOL_RESULT_CHARS + 100))
    session.add(UserMessage.of(huge))
    assert find_keep_from(session.messages, keep_recent_turns=2) is None
    assert find_keep_from(session.messages, keep_recent_turns=0) == len(session.messages)

    def call_llm(messages, tools=None):
        blob = str(messages)
        assert "...[truncated]" in blob
        assert huge not in blob
        return LLMResponse(message=AssistantMessage.of("超长任务摘要"), usage=Usage(10, 4, 14))

    assert try_compact(session, call_llm, CompactPolicy(), max_keep=0) is True
    assert isinstance(session.messages[1], SummaryMessage)
    users = [m for m in session.messages if isinstance(m, UserMessage) and not isinstance(m, SummaryMessage)]
    assert len(users) == 1
    assert users[0].text is not None
    assert users[0].text.endswith("...[truncated]")
    assert len(users[0].text) <= DEFAULT_TOOL_RESULT_CHARS
    assert find_keep_from(session.messages, keep_recent_turns=0) is None


def test_emergency_compact_preserves_task_not_continue_prompt(tmp_path):
    session = Session.create(workspace=tmp_path, system_prompt="sys")
    session.add(UserMessage.of("给 utils.py 加类型标注"))
    session.add(AssistantMessage.of("只写了一半"))
    session.add(ContinueMessage.of(TRUNCATION_CONTINUE_MSG))

    def call_llm(messages, tools=None):
        return LLMResponse(message=AssistantMessage.of("半截回复的摘要"), usage=Usage(8, 4, 12))

    assert try_compact(session, call_llm, CompactPolicy(), max_keep=0) is True
    users = [m for m in session.messages if isinstance(m, UserMessage) and not isinstance(m, SummaryMessage)]
    assert [m.text for m in users] == ["给 utils.py 加类型标注"]
    continues = [m for m in session.messages if isinstance(m, ContinueMessage)]
    assert [m.text for m in continues] == [TRUNCATION_CONTINUE_MSG]
    assert not any(isinstance(m, AssistantMessage) for m in session.messages)


def test_compact_keeps_refreshed_system_prompt(tmp_path):
    session = _three_turns(tmp_path)
    from cyan.session.view import apply_system_prompt

    apply_system_prompt(session, "新系统提示 2026-08-30")

    def call_llm(messages, tools=None):
        return LLMResponse(message=AssistantMessage.of("摘要"), usage=Usage(8, 4, 12))

    assert try_compact(session, call_llm, CompactPolicy()) is True
    assert isinstance(session.messages[0], SystemMessage)
    assert session.messages[0].text == "新系统提示 2026-08-30"


def test_compact_truncates_oversized_summary(tmp_path):
    session = Session.create(workspace=tmp_path, system_prompt="sys")
    session.add(UserMessage.of("做任务"))
    _add_assistant_round(session, "c0", "round-0")
    _add_assistant_round(session, "c1", "round-1")

    def call_llm(messages, tools=None):
        return LLMResponse(
            message=AssistantMessage.of("S" * (DEFAULT_TOOL_RESULT_CHARS + 200)),
            usage=Usage(20, 5, 25),
        )

    assert try_compact(session, call_llm, CompactPolicy()) is True
    assert isinstance(session.messages[1], SummaryMessage)
    text = session.messages[1].text or ""
    assert text.endswith("...[truncated]")
    assert len(text) <= DEFAULT_TOOL_RESULT_CHARS


def test_compact_unmarks_dropped_write_file(tmp_path):
    target = (tmp_path / "a.py").resolve()
    session = Session.create(workspace=tmp_path, system_prompt="sys")
    session.add(UserMessage.of("做任务"))
    session.mark_read(target)
    session.add(
        AssistantMessage.of(
            tool_calls=[ToolCallBlock(id="w0", name="write_file", arguments='{"path": "a.py"}')]
        )
    )
    session.start_tool_execution(call_id="w0", tool_name="write_file", arguments='{"path": "a.py"}')
    session.finish_tool_execution(call_id="w0", ok=True, content="已写入")
    session.add(ToolMessage.of("w0"))
    _add_assistant_round(session, "c1", "kept-read")
    assert session.has_read(target)

    def call_llm(messages, tools=None):
        return LLMResponse(message=AssistantMessage.of("摘要"), usage=Usage(10, 4, 14))

    assert try_compact(session, call_llm, CompactPolicy()) is True
    assert session.tool_history.get("w0") is None
    assert not session.has_read(target)


def test_compact_unmarks_dropped_read_file(tmp_path):
    target = (tmp_path / "a.py").resolve()
    target.write_text("x = 1\n", encoding="utf-8")
    session = Session.create(workspace=tmp_path, system_prompt="sys")
    session.add(UserMessage.of("做任务"))
    session.mark_read(target)
    _add_assistant_round(session, "c0", "old-read")
    _add_assistant_round(session, "c1", "kept-read")
    assert session.has_read(target)

    def call_llm(messages, tools=None):
        return LLMResponse(message=AssistantMessage.of("摘要"), usage=Usage(10, 4, 14))

    assert try_compact(session, call_llm, CompactPolicy()) is True
    assert session.tool_history.get("c0") is None
    assert not session.has_read(target)


def test_compact_unmarks_trailing_comma_arguments(tmp_path):
    target = (tmp_path / "a.py").resolve()
    session = Session.create(workspace=tmp_path, system_prompt="sys")
    session.add(UserMessage.of("做任务"))
    session.mark_read(target)
    session.add(
        AssistantMessage.of(
            tool_calls=[ToolCallBlock(id="c0", name="read_file", arguments='{"path": "a.py",}')]
        )
    )
    session.start_tool_execution(
        call_id="c0", tool_name="read_file", arguments='{"path": "a.py",}'
    )
    session.finish_tool_execution(call_id="c0", ok=True, content="old-read")
    session.add(ToolMessage.of("c0"))
    _add_assistant_round(session, "c1", "kept-read")
    assert session.has_read(target)

    def call_llm(messages, tools=None):
        return LLMResponse(message=AssistantMessage.of("摘要"), usage=Usage(10, 4, 14))

    assert try_compact(session, call_llm, CompactPolicy()) is True
    assert not session.has_read(target)


def test_loop_retries_after_overflow_on_huge_first_user(env, tmp_path):
    huge = "请看这段代码\n" + ("y" * (DEFAULT_TOOL_RESULT_CHARS + 100))
    llm = FakeLLM(
        [AssistantMessage.of("压完后继续。")],
        task_errors=[LLMContextOverflowError("This model's maximum context length is 65536 tokens")],
    )
    runtime = make_runtime(env, llm)
    events, reason = drive(runtime, huge)
    assert reason is StopReason.COMPLETED
    assert llm.compact_requests
    users = [
        m
        for m in runtime.session.messages
        if isinstance(m, UserMessage) and not isinstance(m, SummaryMessage)
    ]
    assert users
    assert users[0].text is not None
    assert len(users[0].text) < len(huge)
    notices = [e.message for e in events if isinstance(e, Notice)]
    assert any("超出模型窗口" in m for m in notices)


def test_skip_when_no_assistant(tmp_path):
    session = Session.create(workspace=tmp_path, system_prompt="sys")
    session.add(UserMessage.of("还没开始"))
    policy = CompactPolicy()

    def boom(messages, tools=None):
        raise AssertionError("不应调用")

    assert resolve_keep_from(session.messages, policy.keep_recent_turns) is None
    assert try_compact(session, boom, policy) is False


def test_compact_single_task_keeps_user_and_recent_rounds(tmp_path):
    session = Session.create(workspace=tmp_path, system_prompt="sys")
    session.add(UserMessage.of("做任务"))
    _add_assistant_round(session, "c0", "round-0-unique")
    _add_assistant_round(session, "c1", "round-1-unique")
    _add_assistant_round(session, "c2", "round-2-unique")

    def call_llm(messages, tools=None):
        return LLMResponse(message=AssistantMessage.of("单任务摘要"), usage=Usage(20, 5, 25))

    assert try_compact(session, call_llm, CompactPolicy()) is True
    assert isinstance(session.messages[0], SystemMessage)
    assert isinstance(session.messages[1], SummaryMessage)
    assert session.messages[1].text == "单任务摘要"
    users = [m for m in session.messages if isinstance(m, UserMessage) and not isinstance(m, SummaryMessage)]
    assert len(users) == 1
    assert users[0].text == "做任务"
    assert session.tool_history.get("c0") is None
    assert session.tool_history.get("c1") is not None
    assert session.tool_history.get("c2") is not None


def test_compact_request_truncates_tool_text(tmp_path):
    session = _three_turns(tmp_path)
    execution = session.tool_history.get("ca")
    assert execution is not None and execution.result is not None
    execution.result.content = "Y" * 40_000
    captured: list[list[dict]] = []

    def call_llm(messages, tools=None):
        captured.append(messages)
        return LLMResponse(message=AssistantMessage.of("摘要"), usage=Usage(8, 4, 12))

    assert try_compact(session, call_llm, CompactPolicy()) is True
    blob = str(captured[0])
    assert "...[truncated]" in blob
    assert "Y" * 40_000 not in blob
    assert "Y" * 100 in blob


def test_compact_replaces_dropped_and_deletes_history(tmp_path):
    session = _three_turns(tmp_path)
    captured: list[tuple[list[dict], list[dict] | None]] = []

    def call_llm(messages, tools=None):
        captured.append((messages, tools))
        return LLMResponse(message=AssistantMessage.of("摘要正文"), usage=Usage(20, 5, 25))

    policy = CompactPolicy()
    assert try_compact(session, call_llm, policy) is True
    assert isinstance(session.messages[0], SystemMessage)
    assert isinstance(session.messages[1], SummaryMessage)
    assert session.messages[1].text == "摘要正文"
    assert session.tool_history.get("ca") is None
    assert session.tool_history.get("cb") is not None
    assert session.tool_history.get("cc") is not None
    assert any(m.text == "任务B" for m in session.messages if isinstance(m, UserMessage))
    assert not any(m.text == "任务A" for m in session.messages if isinstance(m, UserMessage))
    assert any(event.type == "user" and event.payload.get("text") == "任务A" for event in session.events)

    request, tools = captured[0]
    assert tools is None
    assert request[0]["content"] == COMPACT_SYSTEM_PROMPT
    blob = str(request)
    assert "output-A-unique" in blob
    assert "任务A" in blob
    assert "output-B-unique" not in blob
    assert "任务C" not in blob


def test_compact_llm_error_leaves_session(tmp_path):
    session = _three_turns(tmp_path)
    original = list(session.messages)
    history_ids = set(session.tool_history.executions)

    def call_llm(messages, tools=None):
        raise LLMError("boom")

    assert try_compact(session, call_llm, CompactPolicy()) is False
    assert session.messages == original
    assert set(session.tool_history.executions) == history_ids


def test_empty_summary_does_not_mutate(tmp_path):
    session = _three_turns(tmp_path)
    original_len = len(session.messages)

    def call_llm(messages, tools=None):
        return LLMResponse(message=AssistantMessage.of("   "), usage=Usage(3, 0, 3))

    assert try_compact(session, call_llm, CompactPolicy()) is False
    assert len(session.messages) == original_len
    assert session.tool_history.get("ca") is not None


def test_needs_compact_uses_last_prompt_tokens(tmp_path):
    session = _three_turns(tmp_path)
    policy = CompactPolicy(max_context_tokens=1000, reserve_tokens=100, trigger_ratio=0.9)
    assert needs_compact(session, policy) is False
    session.usage.last_prompt_tokens = 900
    assert needs_compact(session, policy) is True


def test_needs_compact_single_task_when_tools_grow(tmp_path):
    session = Session.create(workspace=tmp_path, system_prompt="sys")
    session.add(UserMessage.of("做任务"))
    _add_assistant_round(session, "c0", "x" * 4000)
    _add_assistant_round(session, "c1", "y" * 4000)
    _add_assistant_round(session, "c2", "z" * 4000)
    policy = CompactPolicy(max_context_tokens=1000, reserve_tokens=100, trigger_ratio=0.9)
    session.usage.last_prompt_tokens = 50
    assert find_keep_from(session.messages, policy.keep_recent_turns) is not None
    assert needs_compact(session, policy) is True


def test_needs_compact_when_tools_grew_session(tmp_path):
    """上一轮 prompt 很小，但工具结果已经把当前会话撑过阈值，仍应压缩。"""
    session = _three_turns(tmp_path)
    execution = session.tool_history.get("cc")
    assert execution is not None and execution.result is not None
    execution.result.content = "x" * 8000
    policy = CompactPolicy(max_context_tokens=1000, reserve_tokens=100, trigger_ratio=0.9)
    session.usage.last_prompt_tokens = 100
    assert needs_compact(session, policy) is True


def test_estimate_counts_cjk_heavier_than_ascii():
    ascii_tokens = estimate_payload_tokens([{"role": "user", "content": "a" * 40}])
    cjk_tokens = estimate_payload_tokens([{"role": "user", "content": "中" * 40}])
    assert cjk_tokens > ascii_tokens


def test_needs_compact_prefers_outgoing_wire_estimate(tmp_path):
    session = _three_turns(tmp_path)
    policy = CompactPolicy(max_context_tokens=1000, reserve_tokens=100, trigger_ratio=0.9)
    session.usage.last_prompt_tokens = 100
    assert needs_compact(session, policy, estimated_tokens=50) is False
    assert needs_compact(session, policy, estimated_tokens=900) is True


def test_loop_compacts_before_task_llm(env, tmp_path):
    session = Session.create(workspace=tmp_path, system_prompt="sys")
    _add_turn(session, "任务A", "ca", "output-A-unique")
    _add_turn(session, "任务B", "cb", "output-B-unique")
    session.usage.last_prompt_tokens = 200_000
    llm = FakeLLM([AssistantMessage.of("本轮完成。")])
    runtime = make_runtime(env, llm, session)
    # 默认窗口下 200_000 已高于触发线；这里收紧副本，专门测「出门前先压」。
    # 须高于工具 schema（含 glob/grep），否则压一轮后仍超阈值会把应保留的轮也吃掉。
    runtime.compact_policy.max_context_tokens = 8_000
    runtime.compact_policy.reserve_tokens = 100
    runtime.compact_policy.trigger_ratio = 0.9
    events, reason = drive(runtime, "任务C")
    assert reason is StopReason.COMPLETED
    assert llm.compact_requests
    blob = str(llm.compact_requests[0])
    assert "output-A-unique" in blob
    assert "output-B-unique" not in blob
    assert isinstance(session.messages[1], SummaryMessage)
    assert session.tool_history.get("ca") is None
    assert session.tool_history.get("cb") is not None
    notices = [e.message for e in events if isinstance(e, Notice)]
    assert any("正在压缩" in m for m in notices)
    assert any("已压缩" in m for m in notices)


def test_loop_compacts_when_session_grew_after_last_call(env, tmp_path):
    """工具结果把会话撑大后，即使上一轮 prompt_tokens 很小，下一轮出门前也要压。"""
    session = Session.create(workspace=tmp_path, system_prompt="sys")
    _add_turn(session, "任务A", "ca", "output-A-unique")
    _add_turn(session, "任务B", "cb", "x" * 20_000)
    session.usage.last_prompt_tokens = 50
    llm = FakeLLM([AssistantMessage.of("本轮完成。")])
    runtime = make_runtime(env, llm, session)
    runtime.compact_policy.max_context_tokens = 2_000
    runtime.compact_policy.reserve_tokens = 100
    runtime.compact_policy.trigger_ratio = 0.9
    events, reason = drive(runtime, "任务C")
    assert reason is StopReason.COMPLETED
    assert llm.compact_requests
    assert isinstance(session.messages[1], SummaryMessage)
    notices = [e.message for e in events if isinstance(e, Notice)]
    assert any("正在压缩" in m for m in notices)


def test_loop_compacts_during_single_task(env, tmp_path):
    (tmp_path / "big.txt").write_text("x" * 8000, encoding="utf-8")
    llm = FakeLLM(
        [
            tool_call("read_file", '{"path": "big.txt"}', "r1"),
            tool_call("read_file", '{"path": "big.txt"}', "r2"),
            tool_call("read_file", '{"path": "big.txt"}', "r3"),
            AssistantMessage.of("读完了"),
        ]
    )
    runtime = make_runtime(env, llm)
    runtime.compact_policy.max_context_tokens = 2_000
    runtime.compact_policy.reserve_tokens = 100
    runtime.compact_policy.trigger_ratio = 0.9
    events, reason = drive(runtime, "读大文件")
    assert reason is StopReason.COMPLETED
    assert llm.compact_requests
    assert any(isinstance(m, SummaryMessage) for m in runtime.session.messages)
    users = [
        m
        for m in runtime.session.messages
        if isinstance(m, UserMessage) and not isinstance(m, SummaryMessage)
    ]
    assert any(m.text == "读大文件" for m in users)
    notices = [e.message for e in events if isinstance(e, Notice)]
    assert any("已压缩" in m for m in notices)


def test_slash_compact_command(env, tmp_path):
    session = _three_turns(tmp_path)
    llm = FakeLLM([])
    runtime = make_runtime(env, llm, session)
    renderer = Renderer(Console(file=StringIO(), force_terminal=True))

    class App:
        pass

    app = App()
    app.runtime = runtime
    app.session = session
    app.renderer = renderer
    commands = build_default_commands()
    command = commands.get("/compact")
    assert command is not None
    assert command.handler(app, []) is False
    assert isinstance(session.messages[1], SummaryMessage)
    assert session.tool_history.get("ca") is None
    assert llm.compact_requests


def test_needs_compact_when_only_two_assistant_rounds(tmp_path):
    session = Session.create(workspace=tmp_path, system_prompt="sys")
    session.add(UserMessage.of("做任务"))
    _add_assistant_round(session, "c0", "x" * 4000)
    _add_assistant_round(session, "c1", "y" * 4000)
    policy = CompactPolicy(max_context_tokens=1000, reserve_tokens=100, trigger_ratio=0.9)
    session.usage.last_prompt_tokens = 50
    assert find_keep_from(session.messages, policy.keep_recent_turns) is None
    assert resolve_keep_from(session.messages, policy.keep_recent_turns) is not None
    assert needs_compact(session, policy) is True


def test_loop_compacts_two_rounds_in_one_task(env, tmp_path):
    (tmp_path / "big.txt").write_text("x" * 8000, encoding="utf-8")
    llm = FakeLLM(
        [
            tool_call("read_file", '{"path": "big.txt"}', "r1"),
            tool_call("read_file", '{"path": "big.txt"}', "r2"),
            AssistantMessage.of("读完了"),
        ]
    )
    runtime = make_runtime(env, llm)
    runtime.compact_policy.max_context_tokens = 2_000
    runtime.compact_policy.reserve_tokens = 100
    runtime.compact_policy.trigger_ratio = 0.9
    events, reason = drive(runtime, "读大文件")
    assert reason is StopReason.COMPLETED
    assert llm.compact_requests
    assert any(isinstance(m, SummaryMessage) for m in runtime.session.messages)
    users = [
        m
        for m in runtime.session.messages
        if isinstance(m, UserMessage) and not isinstance(m, SummaryMessage)
    ]
    assert any(m.text == "读大文件" for m in users)
    notices = [e.message for e in events if isinstance(e, Notice)]
    assert any("已压缩" in m for m in notices)


def test_loop_retries_after_context_overflow(env, tmp_path):
    session = Session.create(workspace=tmp_path, system_prompt="sys")
    session.add(UserMessage.of("做任务"))
    _add_assistant_round(session, "c0", "early-round")
    llm = FakeLLM(
        [AssistantMessage.of("压完后继续。")],
        task_errors=[LLMContextOverflowError("This model's maximum context length is 65536 tokens")],
    )
    runtime = make_runtime(env, llm, session)
    events, reason = drive(runtime, "下一问")
    assert reason is StopReason.COMPLETED
    assert llm.compact_requests
    assert isinstance(session.messages[1], SummaryMessage)
    assert session.tool_history.get("c0") is None
    notices = [e.message for e in events if isinstance(e, Notice)]
    assert any("超出模型窗口" in m for m in notices)
    assert any("已压缩" in m for m in notices)


def test_loop_overflow_without_history_is_fatal(env, tmp_path):
    llm = FakeLLM(
        [],
        task_errors=[LLMContextOverflowError("maximum context length exceeded")],
    )
    runtime = make_runtime(env, llm)
    events, reason = drive(runtime, "超大问题")
    assert reason is StopReason.FATAL_ERROR
    notices = [e.message for e in events if isinstance(e, Notice)]
    assert any("无法缩小上下文" in m or "模型调用失败" in m for m in notices)


def test_loop_second_overflow_tries_compact_again(env, tmp_path):
    session = Session.create(workspace=tmp_path, system_prompt="sys")
    session.add(UserMessage.of("做任务"))
    _add_assistant_round(session, "c0", "early-round")
    overflow = LLMContextOverflowError("This model's maximum context length is 65536 tokens")
    llm = FakeLLM(
        [AssistantMessage.of("不应到达")],
        task_errors=[overflow, overflow],
    )
    runtime = make_runtime(env, llm, session)
    events, reason = drive(runtime, "下一问")
    assert reason is StopReason.FATAL_ERROR
    notices = [e.message for e in events if isinstance(e, Notice)]
    assert sum("超出模型窗口" in m for m in notices) == 2


def test_runtime_compact_policy_is_a_copy(make_env, tmp_path):
    env = make_env()
    env.settings.compact.keep_recent_turns = 5
    runtime = make_runtime(env, FakeLLM([]), Session.create(workspace=tmp_path, system_prompt=""))
    assert runtime.compact_policy.keep_recent_turns == 5
    runtime.compact_policy.keep_recent_turns = 1
    assert env.settings.compact.keep_recent_turns == 5
