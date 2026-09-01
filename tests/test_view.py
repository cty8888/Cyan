"""事件表重放：``UserMessage`` 携带的 ``FileBlock`` 要能完整往返。"""

from __future__ import annotations

from cyan.llm.types import FileBlock, TextBlock, UserMessage
from cyan.session import Session
from cyan.session.branch import load_session
from cyan.session.store import DiskStore


def test_user_message_with_file_blocks_persists_and_reloads(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    store = DiskStore.create(workspace, home=home)
    session = Session.create(workspace=workspace, system_prompt="sys", store=store)

    session.add(
        UserMessage(
            blocks=[
                TextBlock(text="看看 @a.py"),
                FileBlock(path="a.py", content="x = 1", start_line=1, end_line=1),
            ]
        )
    )

    reloaded, warning = load_session(workspace, store.session_id, home=home)
    assert warning is None

    user_messages = [m for m in reloaded.messages if isinstance(m, UserMessage)]
    assert len(user_messages) == 1
    files = user_messages[0].file_blocks
    assert len(files) == 1
    assert files[0].path == "a.py"
    assert files[0].content == "x = 1"
    assert files[0].start_line == 1
    assert files[0].end_line == 1
    assert user_messages[0].text == "看看 @a.py"


def test_user_message_without_file_blocks_still_roundtrips(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    store = DiskStore.create(workspace, home=home)
    session = Session.create(workspace=workspace, system_prompt="sys", store=store)

    session.add(UserMessage.of("纯文本任务"))

    reloaded, _ = load_session(workspace, store.session_id, home=home)
    user_messages = [m for m in reloaded.messages if isinstance(m, UserMessage)]
    assert len(user_messages) == 1
    assert user_messages[0].file_blocks == []
    assert user_messages[0].text == "纯文本任务"
