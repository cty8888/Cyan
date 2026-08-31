"""会话：重复调用指纹与进展重置。"""

from __future__ import annotations

from cyan.session import Session, TodoItem, TodoStatus
from cyan.session.store import DiskStore


def test_consecutive_identical_calls_increment(tmp_path):
    session = Session.create(workspace=tmp_path, system_prompt="")
    assert session.record_call_fingerprint("read_file", {"path": "a.py"}) == 1
    assert session.record_call_fingerprint("read_file", {"path": "a.py"}) == 2
    assert session.record_call_fingerprint("read_file", {"path": "a.py"}) == 3


def test_alternating_calls_reset_streak(tmp_path):
    session = Session.create(workspace=tmp_path, system_prompt="")
    counts = []
    for _ in range(3):
        counts.append(session.record_call_fingerprint("read_file", {"path": "a.py"}))
        counts.append(session.record_call_fingerprint("read_file", {"path": "b.py"}))
    assert max(counts) == 1


def test_reset_repeat_tracking_clears_counter(tmp_path):
    session = Session.create(workspace=tmp_path, system_prompt="")
    counts = []
    for _ in range(4):
        counts.append(session.record_call_fingerprint("bash", {"command": "pytest"}))
        session.record_call_fingerprint("edit_file", {"path": "x.py"})
        session.reset_repeat_tracking()
    assert max(counts) == 1


def test_set_todos_updates_state_and_persists(tmp_path):
    store = DiskStore.create(tmp_path, home=tmp_path / "home")
    session = Session.create(workspace=tmp_path, system_prompt="", store=store)
    items = [
        TodoItem(content="写测试", status=TodoStatus.IN_PROGRESS, active_form="正在写测试"),
        TodoItem(content="补文档", status=TodoStatus.PENDING),
    ]

    session.set_todos(items)

    assert session.todos == items
    meta = store.load_meta()
    assert meta is not None
    assert meta.todos == [item.to_json() for item in items]


def test_set_todos_empty_list_clears(tmp_path):
    session = Session.create(workspace=tmp_path, system_prompt="")
    session.set_todos([TodoItem(content="a")])
    session.set_todos([])
    assert session.todos == []
