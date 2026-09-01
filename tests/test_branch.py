"""compact overlay 与 fork：视图变短，事件表保留原文。"""

from __future__ import annotations

from cyan.llm.types import AssistantMessage, LLMResponse, SummaryMessage, Usage, UserMessage
from cyan.session.branch import fork_at_user, user_event_entries
from cyan.session.compact import CompactPolicy, try_compact
from cyan.session.events import USER
from cyan.session.store import DiskStore

from .test_compact import _three_turns


def test_overlay_keeps_events_after_compact(tmp_path):
    session = _three_turns(tmp_path)

    def call_llm(messages, tools=None):
        return LLMResponse(message=AssistantMessage.of("摘要正文"), usage=Usage(8, 4, 12))

    assert try_compact(session, call_llm, CompactPolicy()) is True
    assert isinstance(session.messages[1], SummaryMessage)
    assert any(event.type == USER and event.payload.get("text") == "任务A" for event in session.events)
    users = [event.payload.get("text") for event in session.events if event.type == USER]
    assert "任务A" in users
    assert "任务B" in users


def test_fork_copies_source_not_compact(tmp_path):
    session = _three_turns(tmp_path)

    def call_llm(messages, tools=None):
        return LLMResponse(message=AssistantMessage.of("旧摘要"), usage=Usage(8, 4, 12))

    assert try_compact(session, call_llm, CompactPolicy()) is True
    entries = user_event_entries(session)
    first = entries[0][1]
    branched = fork_at_user(session, first.id)
    assert branched.metadata.parent_id == session.metadata.session_id
    texts = [event.payload.get("text") for event in branched.events if event.type == USER]
    assert texts == ["任务A"]
    assert not any(event.type == "compact" for event in branched.events)
    users = [m.text for m in branched.messages if isinstance(m, UserMessage) and not isinstance(m, SummaryMessage)]
    assert users == ["任务A"]
    assert session.events[-1].type == "compact"


def test_summarize_from_keeps_earlier_users(tmp_path):
    session = _three_turns(tmp_path)
    from cyan.session.branch import view_index_for_user_event

    entries = user_event_entries(session)
    target = entries[1][1]
    index = view_index_for_user_event(session, target.id)
    assert index is not None
    end = len(session.messages) - 1

    def call_llm(messages, tools=None):
        return LLMResponse(message=AssistantMessage.of("后半摘要"), usage=Usage(8, 4, 12))

    assert try_compact(
        session,
        call_llm,
        CompactPolicy(),
        keep_from=index,
        drop_end=end,
        reason="summarize_from",
    )
    users = [
        m.text
        for m in session.messages
        if isinstance(m, UserMessage) and not isinstance(m, SummaryMessage)
    ]
    assert "任务A" in users
    assert "任务B" not in users
    assert any(event.payload.get("text") == "任务B" for event in session.events if event.type == USER)


def test_disk_roundtrip_after_compact(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("CYAN_HOME", str(home))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    store = DiskStore.create(workspace, home=home)
    session = _three_turns(workspace)
    session.store = store
    session.metadata.session_id = store.session_id
    for event in session.events:
        store.append(event)
    store.set_last()

    def call_llm(messages, tools=None):
        return LLMResponse(message=AssistantMessage.of("磁盘摘要"), usage=Usage(8, 4, 12))

    assert try_compact(session, call_llm, CompactPolicy()) is True
    from cyan.session.branch import load_session

    loaded, warning = load_session(workspace, store.session_id, home=home)
    assert warning is None
    assert isinstance(loaded.messages[1], SummaryMessage)
    assert any(event.payload.get("text") == "任务A" for event in loaded.events if event.type == USER)


def test_repair_unpaired_tool_calls(tmp_path):
    from cyan.llm.types import ToolCallBlock, ToolMessage
    from cyan.session import Session
    from cyan.session.view import repair_unpaired_tool_calls

    session = Session.create(workspace=tmp_path, system_prompt="sys")
    session.add(UserMessage.of("读一下"))
    session.add(
        AssistantMessage.of(tool_calls=[ToolCallBlock(id="c1", name="read_file", arguments='{"path": "a.py"}')])
    )
    repair_unpaired_tool_calls(session)
    assert session.tool_history.get("c1") is not None
    assert any(isinstance(message, ToolMessage) for message in session.messages)


def test_fork_restores_checkpoint_at_user(tmp_path):
    from cyan.session import Session

    session = Session.create(workspace=tmp_path, system_prompt="sys")
    session.add(UserMessage.of("任务1"))
    src = tmp_path / "src"
    src.mkdir()
    session.workspace.cwd = src
    target = tmp_path / "a.py"
    target.write_text("x", encoding="utf-8")
    session.mark_read(target)
    session.permissions.always_allowed.add("exec:pytest")
    session.add(UserMessage.of("任务2"))
    first = user_event_entries(session)[0][1]
    early = fork_at_user(session, first.id)
    assert early.workspace.cwd == tmp_path.resolve()
    assert target.resolve() not in early.workspace.opened_files
    second = user_event_entries(session)[1][1]
    branched = fork_at_user(session, second.id)
    assert branched.workspace.cwd == src.resolve()
    assert target.resolve() in branched.workspace.opened_files
    assert "exec:pytest" in branched.permissions.always_allowed


def test_fork_restores_todos_checkpoint(tmp_path):
    """checkpoint 挂在 user 事件后面，用当时的 ``session.todos`` 拍照——
    ``set_todos()`` 本身不追加新 checkpoint，要等下一条用户消息才把新状态定住。
    """
    from cyan.session import Session, TodoItem, TodoStatus

    session = Session.create(workspace=tmp_path, system_prompt="sys")
    session.add(UserMessage.of("任务1"))  # checkpoint 1：todos 还是空
    session.set_todos([TodoItem(content="第一步", status=TodoStatus.COMPLETED)])
    session.add(UserMessage.of("任务2"))  # checkpoint 2：捕到「第一步」
    session.set_todos([TodoItem(content="第二步", status=TodoStatus.IN_PROGRESS, active_form="正在做第二步")])

    entries = user_event_entries(session)
    first = entries[0][1]
    early = fork_at_user(session, first.id)
    assert early.todos == []

    second = entries[1][1]
    branched = fork_at_user(session, second.id)
    assert [item.content for item in branched.todos] == ["第一步"]


def test_disk_roundtrip_preserves_todos(tmp_path, monkeypatch):
    from cyan.session import Session, TodoItem, TodoStatus
    from cyan.session.branch import load_session

    home = tmp_path / "home"
    monkeypatch.setenv("CYAN_HOME", str(home))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    store = DiskStore.create(workspace, home=home)
    session = Session.create(workspace=workspace, system_prompt="sys", store=store)
    session.set_todos([TodoItem(content="写测试", status=TodoStatus.IN_PROGRESS, active_form="正在写测试")])

    loaded, warning = load_session(workspace, store.session_id, home=home)
    assert warning is None
    assert [item.to_json() for item in loaded.todos] == [item.to_json() for item in session.todos]


def test_oversized_user_survives_reload(tmp_path, monkeypatch):
    from cyan.session import Session
    from cyan.session.branch import load_session
    from cyan.settings.tools import DEFAULT_TOOL_RESULT_CHARS

    home = tmp_path / "home"
    monkeypatch.setenv("CYAN_HOME", str(home))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    store = DiskStore.create(workspace, home=home)
    session = Session.create(workspace=workspace, system_prompt="sys", store=store)
    huge = "贴一段代码\n" + ("x" * (DEFAULT_TOOL_RESULT_CHARS + 100))
    session.add(UserMessage.of(huge))

    def call_llm(messages, tools=None):
        return LLMResponse(message=AssistantMessage.of("超长任务摘要"), usage=Usage(10, 4, 14))

    assert try_compact(session, call_llm, CompactPolicy(), max_keep=0) is True
    live = [
        m
        for m in session.messages
        if isinstance(m, UserMessage) and not isinstance(m, SummaryMessage)
    ]
    assert len(live) == 1
    assert live[0].text is not None and live[0].text.endswith("...[truncated]")
    assert len(live[0].text) <= DEFAULT_TOOL_RESULT_CHARS

    loaded, warning = load_session(workspace, store.session_id, home=home)
    assert warning is None
    restored = [
        m
        for m in loaded.messages
        if isinstance(m, UserMessage) and not isinstance(m, SummaryMessage)
    ]
    assert len(restored) == 1
    assert restored[0].text == live[0].text
