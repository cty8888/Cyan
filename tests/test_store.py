"""会话 jsonl 存储：路径编码、只追加、last 指针。"""

from __future__ import annotations

from cyan.llm.types import UserMessage
from cyan.session import Session
from cyan.session.events import USER, SessionEvent
from cyan.session.paths import encode_workspace
from cyan.session.store import DiskStore, latest_jsonl_id, list_sessions, read_last, resolve_session_id


def test_encode_posix_and_windows_paths(tmp_path):
    workspace = tmp_path / "proj"
    workspace.mkdir()
    encoded = encode_workspace(workspace)
    assert "/" not in encoded and "\\" not in encoded and ":" not in encoded
    assert encoded.endswith("proj") or encoded.endswith("-proj")
    windows = "C:\\Users\\lenovo".replace("-", "--").replace(":", "-").replace("/", "-").replace("\\", "-")
    assert windows == "C--Users-lenovo"


def test_encode_distinguishes_slash_and_hyphen(tmp_path):
    hyphen = tmp_path / "foo-bar"
    nested = tmp_path / "foo" / "bar"
    hyphen.mkdir()
    nested.mkdir(parents=True)
    assert encode_workspace(hyphen) != encode_workspace(nested)
    assert encode_workspace(hyphen).endswith("foo--bar")
    assert encode_workspace(nested).endswith("foo-bar")


def test_jsonl_roundtrip_and_skip_bad_last_line(tmp_path, monkeypatch):
    monkeypatch.setenv("CYAN_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    store = DiskStore.create(workspace, home=tmp_path / "home")
    event = SessionEvent(type=USER, payload={"text": "hello"})
    store.append(event)
    loaded = store.load_events()
    assert len(loaded) == 1
    assert loaded[0].payload["text"] == "hello"

    with store.jsonl.open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    loaded = store.load_events()
    assert len(loaded) == 1


def test_last_pointer_and_list(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("CYAN_HOME", str(home))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    first = DiskStore.create(workspace, home=home)
    first.append(SessionEvent(type=USER, payload={"text": "a"}))
    first.set_last()
    second = DiskStore.create(workspace, home=home)
    second.append(SessionEvent(type=USER, payload={"text": "b"}))
    second.set_last()
    assert read_last(workspace, home=home) == second.session_id
    items = list_sessions(workspace, home=home)
    assert {item.session_id for item in items} == {first.session_id, second.session_id}
    assert resolve_session_id(workspace, second.session_id[:8], home=home) == second.session_id
    assert latest_jsonl_id(workspace, home=home) in {first.session_id, second.session_id}


def test_jsonl_is_sibling_of_sidecar_dir(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    store = DiskStore.create(workspace, home=home)
    store.append(SessionEvent(type=USER, payload={"text": "x"}))
    assert store.jsonl.parent == store.sidecar.parent
    assert store.jsonl.name == f"{store.session_id}.jsonl"
    assert store.sidecar.name == store.session_id
    assert store.jsonl.is_file()
    assert store.sidecar.is_dir()


def test_new_session_does_not_steal_last_until_user_speaks(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("CYAN_HOME", str(home))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    real = DiskStore.create(workspace, home=home)
    session = Session.create(workspace=workspace, system_prompt="sys", store=real)
    session.add(UserMessage.of("真正的任务"))
    assert read_last(workspace, home=home) == real.session_id
    empty = DiskStore.create(workspace, home=home)
    Session.create(workspace=workspace, system_prompt="sys", store=empty)
    assert read_last(workspace, home=home) == real.session_id


def test_continue_skips_empty_last_session(tmp_path, monkeypatch):
    from cyan.session.branch import continue_session

    home = tmp_path / "home"
    monkeypatch.setenv("CYAN_HOME", str(home))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    real = DiskStore.create(workspace, home=home)
    session = Session.create(workspace=workspace, system_prompt="sys", store=real)
    session.add(UserMessage.of("真正的任务"))
    empty = DiskStore.create(workspace, home=home)
    Session.create(workspace=workspace, system_prompt="sys", store=empty)
    empty.set_last()
    loaded, warning = continue_session(workspace, home=home)
    assert warning is None
    assert loaded.metadata.session_id == real.session_id
