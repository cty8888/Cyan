"""加载、fork、从事件表列出用户轮次。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from ..llm.types import SummaryMessage, UserMessage
from ..security.types import PermissionMode
from .events import BRANCH_FORKED, CHECKPOINT, FORK_COPY_TYPES, SUMMARY, USER, SessionEvent, new_event_id
from .session import Session
from .store import DiskStore
from .types import SessionMetadata, SessionPermissions, SessionWorkspace
from .view import apply_checkpoint, apply_meta, rebuild_messages, repair_unpaired_tool_calls


def load_session(
    workspace: Path,
    session_id: str,
    *,
    home: Path | None = None,
) -> tuple[Session, str | None]:
    """从磁盘重放会话。第二个返回值是 workspace 不一致时的警告。"""
    store = DiskStore.create(workspace, home=home, session_id=session_id)
    events = store.load_events()
    meta = store.load_meta()
    permission_mode = PermissionMode.DEFAULT
    if meta is not None:
        try:
            permission_mode = PermissionMode(meta.permission_mode)
        except ValueError:
            permission_mode = PermissionMode.DEFAULT
    session = Session(
        workspace=SessionWorkspace.for_root(workspace),
        metadata=SessionMetadata(
            session_id=session_id,
            created_at=meta.created_at if meta else 0.0,
            updated_at=meta.updated_at if meta else 0.0,
            title=meta.title if meta else None,
            parent_id=meta.parent_id if meta else None,
            forked_from_event_id=meta.forked_from_event_id if meta else None,
        ),
        permissions=SessionPermissions(permission_mode=permission_mode),
        events=events,
        store=store,
    )
    rebuild_messages(session)
    warning = None
    if meta is not None:
        apply_meta(session, meta)
        recorded = (meta.workspace or "").strip()
        if recorded:
            try:
                if Path(recorded).resolve() != Path(workspace).resolve():
                    warning = f"会话记录的工作目录是 {recorded}，当前是 {workspace}"
            except OSError:
                warning = f"会话记录的工作目录是 {recorded}，当前是 {workspace}"
    repair_unpaired_tool_calls(session)
    store.set_last()
    return session, warning


def continue_session(workspace: Path, *, home: Path | None = None) -> tuple[Session, str | None] | None:
    """读 last；空会话（还没有用户消息）跳过，改用最近一次有过对话的。"""
    from .store import list_sessions, read_last, session_has_user

    last = read_last(workspace, home=home)
    if last and store_jsonl_exists(workspace, last, home=home) and session_has_user(
        workspace, last, home=home
    ):
        return load_session(workspace, last, home=home)
    for item in list_sessions(workspace, home=home):
        if item.session_id == last:
            continue
        if session_has_user(workspace, item.session_id, home=home):
            return load_session(workspace, item.session_id, home=home)
    return None


def store_jsonl_exists(workspace: Path, session_id: str, *, home: Path | None = None) -> bool:
    from .paths import events_path

    return events_path(workspace, session_id, home=home).is_file()


def user_event_entries(session: Session) -> list[tuple[int, SessionEvent]]:
    """完整日志里的用户任务（不含 summary/continue），供 /history 与 rewind。"""
    entries: list[tuple[int, SessionEvent]] = []
    number = 0
    for event in session.events:
        if event.type != USER:
            continue
        number += 1
        entries.append((number, event))
    return entries


def resolve_user_anchor(session: Session, token: str) -> SessionEvent | None:
    """序号或事件 id / 前缀。"""
    token = token.strip()
    entries = user_event_entries(session)
    if token.isdigit():
        index = int(token)
        for number, event in entries:
            if number == index:
                return event
        return None
    exact = [event for _, event in entries if event.id == token]
    if exact:
        return exact[0]
    prefix = [event for _, event in entries if event.id.startswith(token)]
    if len(prefix) == 1:
        return prefix[0]
    return None


def fork_at_user(session: Session, user_event_id: str) -> Session:
    """拷贝锚点及之前的源事件到新 session。不改父 jsonl。"""
    copied: list[SessionEvent] = []
    found = False
    for event in session.events:
        if event.type not in FORK_COPY_TYPES:
            continue
        if found:
            if event.type == CHECKPOINT and str(event.payload.get("after_event_id") or "") == user_event_id:
                copied.append(event)
            break
        copied.append(event)
        if event.id == user_event_id:
            found = True
    if not found:
        raise ValueError(f"找不到用户消息 {user_event_id}")

    new_store = None
    if session.store is not None:
        new_store = DiskStore.create(session.workspace.root, home=session.store.home)

    metadata = SessionMetadata.create(title=session.metadata.title)
    if new_store is not None:
        metadata.session_id = new_store.session_id
    metadata.parent_id = session.metadata.session_id
    metadata.forked_from_event_id = user_event_id

    new_session = Session(
        workspace=SessionWorkspace.for_root(session.workspace.root),
        metadata=metadata,
        permissions=SessionPermissions(permission_mode=session.permissions.permission_mode),
        store=new_store,
        model=session.model,
    )

    branch = SessionEvent(
        type=BRANCH_FORKED,
        payload={
            "parent_session_id": session.metadata.session_id,
            "fork_event_id": user_event_id,
        },
    )
    new_events = [branch]
    id_map: dict[str, str] = {}
    parent_id = branch.id
    for event in copied:
        new_id = new_event_id()
        id_map[event.id] = new_id
        payload = deepcopy(event.payload)
        payload["source_id"] = event.id
        after = payload.get("after_event_id")
        if event.type == CHECKPOINT and after in id_map:
            payload["after_event_id"] = id_map[str(after)]
        new_events.append(
            SessionEvent(
                type=event.type,
                payload=payload,
                id=new_id,
                parent_id=parent_id,
                ts=event.ts,
            )
        )
        parent_id = new_id

    new_session.events = new_events
    if new_store is not None:
        new_store.write_events(new_events)
    rebuild_messages(new_session)
    apply_checkpoint(new_session, new_events, id_map[user_event_id])
    new_session.persist_head()
    new_session._mark_last()
    return new_session


def view_index_for_user_event(session: Session, user_event_id: str) -> int | None:
    """当前视图里对应的 UserMessage 下标；已被 compact 藏掉则 None。"""
    for index, message in enumerate(session.messages):
        if isinstance(message, UserMessage) and not isinstance(message, SummaryMessage) and message.id == user_event_id:
            return index
    return None
