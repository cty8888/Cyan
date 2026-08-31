"""项目级 Auto Memory：存储、工具、结束提取。"""

from __future__ import annotations

from pathlib import Path

import pytest

from cyan.core.runtime import Runtime
from cyan.core.types import AssistantReply, Notice, StopReason, TaskFinished
from cyan.errors import LLMError, PathOutsideWorkspaceError, ToolError
from cyan.llm.types import AssistantMessage, UserMessage
from cyan.memory.extract import persist_auto_memory
from cyan.memory.store import (
    list_memory_files,
    read_memory_file,
    resolve_memory_file,
    write_entry,
)
from cyan.memory.types import INDEX_FILENAME, MemoryEntry, MemoryKind
from cyan.prompt.stack import PromptStack
from cyan.security.types import PermissionMode
from cyan.session import Session
from cyan.settings.tools import DEFAULT_TOOL_RESULT_CHARS

from .conftest import FakeLLM, drive, eval_perm, tool_call


def _stack(workspace: Path) -> PromptStack:
    return PromptStack(workspace=workspace, auto_memory=True)


def _runtime_with_memory(env, llm, session=None):
    session = session or Session.create(workspace=env.settings.workspace, system_prompt="sys")
    return Runtime.create(
        env.settings,
        llm,
        env.registry,
        env.permissions,
        session,
        prompt_stack=_stack(env.settings.workspace),
    )


def test_write_entry_and_index(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    wrote = write_entry(
        workspace,
        MemoryEntry(kind=MemoryKind.FEEDBACK, summary="不要用 find", detail="用 glob/grep"),
    )
    assert wrote
    index = read_memory_file(workspace, INDEX_FILENAME)
    assert "- [feedback] 不要用 find" in index
    assert "用 glob/grep" in read_memory_file(workspace, "feedback.md")
    names = {name for name, _size in list_memory_files(workspace)}
    assert INDEX_FILENAME in names
    assert "feedback.md" in names


def test_duplicate_summary_or_cyan_md_is_skipped(tmp_path):
    workspace = tmp_path / "ws"
    (workspace / ".cyan").mkdir(parents=True)
    (workspace / ".cyan" / "cyan.md").write_text("已经写在规范里的句子", encoding="utf-8")
    assert not write_entry(
        workspace,
        MemoryEntry(kind=MemoryKind.PROJECT, summary="已经写在规范里的句子"),
    )
    write_entry(workspace, MemoryEntry(kind=MemoryKind.USER, summary="用 tabs"))
    assert not write_entry(workspace, MemoryEntry(kind=MemoryKind.USER, summary="用 tabs"))


def test_illegal_filename_rejected(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    try:
        resolve_memory_file(workspace, "notes.md")
        assert False, "should reject"
    except ToolError as exc:
        assert "notes.md" in str(exc)


def test_symlink_escape_rejected(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    leak = tmp_path / "leak.md"
    leak.write_text("LEAK", encoding="utf-8")
    target = workspace / ".cyan" / "memory"
    target.mkdir(parents=True)
    (target / "MEMORY.md").symlink_to(leak)
    try:
        resolve_memory_file(workspace, "MEMORY.md")
        assert False, "should reject"
    except PathOutsideWorkspaceError:
        pass


def test_memory_write_skips_approval(env):
    tool = env.registry.get("memory_write")
    outcome = eval_perm(env, tool, {"kind": "feedback", "content": "以后用 pytest"})
    assert outcome.kind == "allow"
    result = env.registry.execute(
        "memory_write",
        {"kind": "feedback", "content": "以后用 pytest"},
        env.ctx,
    )
    assert result.ok
    assert (env.settings.workspace / ".cyan" / "memory" / "feedback.md").is_file()


def test_memory_write_denied_in_plan(env):
    tool = env.registry.get("memory_write")
    outcome = eval_perm(
        env, tool, {"kind": "feedback", "content": "x"}, mode=PermissionMode.PLAN
    )
    assert outcome.kind == "deny"


def test_write_file_to_memory_still_needs_approval(env):
    outcome = eval_perm(
        env,
        env.registry.get("write_file"),
        {"path": ".cyan/memory/user.md", "content": "x"},
    )
    assert outcome.kind == "need_approval"


def test_completed_triggers_extract(env):
    llm = FakeLLM(
        [AssistantMessage.of("做完了")],
        extract_script=['{"entries": [{"kind": "feedback", "summary": "测试要跑 pytest"}]}'],
    )
    runtime = _runtime_with_memory(env, llm)
    events, reason = drive(runtime, "随便做一下")
    assert reason is StopReason.COMPLETED
    assert llm.extract_requests
    index = env.settings.workspace / ".cyan" / "memory" / "MEMORY.md"
    assert index.is_file()
    assert "pytest" in index.read_text(encoding="utf-8")
    assert any(getattr(event, "message", "") == "已写入 1 条自动记忆。" for event in events) or any(
        "已写入 1 条" in getattr(event, "message", "") for event in events
    )


def test_abort_does_not_extract(env):
    llm = FakeLLM([tool_call("list_dir", '{"path": "."}')])
    runtime = _runtime_with_memory(env, llm)
    stream = runtime.run("中断")
    reply = None
    while True:
        event = stream.send(reply)
        reply = None
        if isinstance(event, AssistantReply) or event.__class__.__name__ == "Thinking":
            try:
                thrown = stream.throw(KeyboardInterrupt())
            except (StopIteration, KeyboardInterrupt):
                thrown = None
            if isinstance(thrown, TaskFinished):
                assert thrown.reason is StopReason.USER_ABORT
                stream.close()
            break
    assert llm.extract_requests == []
    assert not (env.settings.workspace / ".cyan" / "memory" / "MEMORY.md").exists()


def test_extract_skips_cyan_md_duplicate(env):
    cyan = env.settings.workspace / ".cyan" / "cyan.md"
    cyan.parent.mkdir(parents=True, exist_ok=True)
    cyan.write_text("规范：始终跑 pytest\n", encoding="utf-8")
    session = Session.create(workspace=env.settings.workspace, system_prompt="sys")
    session.state.current_task = "改测试"
    llm = FakeLLM([])
    llm.extract_script = [
        '{"entries": [{"kind": "feedback", "summary": "始终跑 pytest"}]}'
    ]
    written = persist_auto_memory(session, llm.chat)
    assert written == 0
    assert not (env.settings.workspace / ".cyan" / "memory" / "MEMORY.md").exists()


def test_session_started_excludes_memory_body(tmp_path):
    from cyan.session.events import SESSION_STARTED
    from cyan.session.store import DiskStore

    workspace = tmp_path / "ws"
    workspace.mkdir()
    memory = workspace / ".cyan" / "memory"
    memory.mkdir(parents=True)
    (memory / "MEMORY.md").write_text("- [user] 秘密偏好\n", encoding="utf-8")
    store = DiskStore.create(workspace, home=tmp_path / "home")
    session = Session.create(workspace=workspace, system_prompt="identity-only", store=store)
    raw = store.jsonl.read_text(encoding="utf-8")
    assert "秘密偏好" not in raw
    started = [event for event in session.events if event.type == SESSION_STARTED]
    assert "秘密偏好" not in str(started[0].payload)
    assert session.messages[0].text == "identity-only"


def test_extract_only_current_task(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    session = Session.create(workspace=workspace, system_prompt="sys")
    session.add(UserMessage.of("旧任务"))
    session.add(AssistantMessage.of("旧回复 UNIQUE_OLD_TASK"))
    session.add(UserMessage.of("新任务"))
    session.state.current_task = "新任务"
    session.add(AssistantMessage.of("新回复"))
    llm = FakeLLM([])
    llm.extract_script = ['{"entries": []}']
    persist_auto_memory(session, llm.chat)
    assert llm.extract_requests
    blob = llm.extract_requests[0][1]["content"]
    assert "UNIQUE_OLD_TASK" not in blob
    assert "新任务" in blob


def test_extract_truncates_from_head(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    session = Session.create(workspace=workspace, system_prompt="sys")
    huge = "HEAD_MARKER " + ("x" * (DEFAULT_TOOL_RESULT_CHARS + 50)) + " TAIL_MARKER"
    session.add(UserMessage.of(huge))
    session.state.current_task = "任务"
    llm = FakeLLM([])
    llm.extract_script = ['{"entries": []}']
    persist_auto_memory(session, llm.chat)
    blob = llm.extract_requests[0][1]["content"]
    assert "HEAD_MARKER" in blob
    assert "TAIL_MARKER" not in blob
    assert "...[truncated]" in blob


def test_extract_parses_fenced_json(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    session = Session.create(workspace=workspace, system_prompt="sys")
    session.add(UserMessage.of("记住我喜欢 tabs"))
    session.state.current_task = "记住我喜欢 tabs"
    llm = FakeLLM([])
    llm.extract_script = [
        '```json\n{"entries": [{"kind": "user", "summary": "喜欢 tabs"}]}\n```'
    ]
    written = persist_auto_memory(session, llm.chat)
    assert written == 1
    index = read_memory_file(workspace, INDEX_FILENAME)
    assert "喜欢 tabs" in index


def test_extract_llm_error_propagates(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    session = Session.create(workspace=workspace, system_prompt="sys")
    session.add(UserMessage.of("任务"))
    session.state.current_task = "任务"

    def boom(messages, tools=None):
        raise LLMError("down")

    with pytest.raises(LLMError):
        persist_auto_memory(session, boom)


def test_completed_extract_failure_is_noticed(env):
    class ExtractBoom(FakeLLM):
        def chat(self, messages, tools=None):
            if messages and "值得跨会话记住" in str(messages[0].get("content")):
                self.extract_requests.append(list(messages))
                raise LLMError("down")
            return super().chat(messages, tools)

    llm = ExtractBoom([AssistantMessage.of("做完了")])
    runtime = _runtime_with_memory(env, llm)
    events, reason = drive(runtime, "随便做一下")
    assert reason is StopReason.COMPLETED
    notices = [getattr(event, "message", "") for event in events if isinstance(event, Notice)]
    assert any("自动记忆提取失败" in message for message in notices)
