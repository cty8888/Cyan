"""磁盘镜像：``<id>.jsonl`` 只追加，sidecar ``meta.json`` 与 ``last`` 可改写。"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .events import USER, SessionEvent
from .paths import (
    cyan_home,
    ensure_secure_dir,
    ensure_secure_file,
    events_path,
    last_path,
    project_dir,
    settings_path,
    sidecar_dir,
)

META_VERSION = 1
SETTINGS_VERSION = 1


def atomic_write(target: Path, text: str, *, mode: int = 0o600) -> None:
    """temp + replace，避免写一半留下坏文件。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)
    ensure_secure_file(target, mode=mode)


@dataclass
class SessionMeta:
    """sidecar meta.json：身份 + 最新 head 状态。"""

    v: int = META_VERSION
    id: str = ""
    title: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    workspace: str = ""
    parent_id: str | None = None
    forked_from_event_id: str | None = None
    cwd: str | None = None
    opened_files: list[str] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)
    always_allowed: list[str] = field(default_factory=list)
    permission_mode: str = "default"
    todos: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "v": self.v,
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "workspace": self.workspace,
            "parent_id": self.parent_id,
            "forked_from_event_id": self.forked_from_event_id,
            "cwd": self.cwd,
            "opened_files": self.opened_files,
            "modified_files": self.modified_files,
            "always_allowed": self.always_allowed,
            "permission_mode": self.permission_mode,
            "todos": self.todos,
            "usage": self.usage,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SessionMeta:
        usage = data.get("usage") or {}
        todos = data.get("todos") or []
        return cls(
            v=int(data.get("v") or META_VERSION),
            id=str(data.get("id") or ""),
            title=data.get("title"),
            created_at=float(data.get("created_at") or 0.0),
            updated_at=float(data.get("updated_at") or 0.0),
            workspace=str(data.get("workspace") or ""),
            parent_id=data.get("parent_id"),
            forked_from_event_id=data.get("forked_from_event_id"),
            cwd=data.get("cwd"),
            opened_files=list(data.get("opened_files") or []),
            modified_files=list(data.get("modified_files") or []),
            always_allowed=list(data.get("always_allowed") or []),
            permission_mode=str(data.get("permission_mode") or "default"),
            todos=list(todos) if isinstance(todos, list) else [],
            usage={str(k): int(v) for k, v in usage.items()} if isinstance(usage, dict) else {},
        )


@dataclass
class DiskStore:
    """一个 session 的磁盘位置。``append`` 只追加 jsonl。"""

    workspace: Path
    session_id: str
    home: Path

    @classmethod
    def create(cls, workspace: Path, *, home: Path | None = None, session_id: str | None = None) -> DiskStore:
        root = home if home is not None else cyan_home()
        ensure_secure_dir(root)
        ensure_secure_dir(root / "projects")
        settings = settings_path(home=root)
        if not settings.is_file():
            atomic_write(settings, json.dumps({"v": SETTINGS_VERSION}, ensure_ascii=False) + "\n")
        sid = session_id or str(uuid.uuid4())
        store = cls(workspace=Path(workspace).resolve(), session_id=sid, home=root)
        ensure_secure_dir(store.project_dir)
        ensure_secure_dir(store.sidecar)
        ensure_secure_dir(store.sidecar / "snapshots")
        return store

    @property
    def project_dir(self) -> Path:
        return project_dir(self.workspace, home=self.home)

    @property
    def jsonl(self) -> Path:
        return events_path(self.workspace, self.session_id, home=self.home)

    @property
    def sidecar(self) -> Path:
        return sidecar_dir(self.workspace, self.session_id, home=self.home)

    @property
    def meta_path(self) -> Path:
        return self.sidecar / "meta.json"

    def append(self, event: SessionEvent) -> None:
        line = json.dumps(event.to_json(), ensure_ascii=False) + "\n"
        self.project_dir.mkdir(parents=True, exist_ok=True)
        with self.jsonl.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        ensure_secure_file(self.jsonl)

    def load_events(self) -> list[SessionEvent]:
        if not self.jsonl.is_file():
            return []
        events: list[SessionEvent] = []
        lines = self.jsonl.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            text = line.strip()
            if not text:
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                if index == len(lines) - 1:
                    continue
                raise
            if isinstance(data, dict):
                events.append(SessionEvent.from_json(data))
        return events

    def write_events(self, events: list[SessionEvent]) -> None:
        """fork 时一次性写入新 jsonl（仍按行 json）。"""
        body = "".join(json.dumps(event.to_json(), ensure_ascii=False) + "\n" for event in events)
        atomic_write(self.jsonl, body)

    def write_meta(self, meta: SessionMeta) -> None:
        atomic_write(self.meta_path, json.dumps(meta.to_json(), ensure_ascii=False, indent=2) + "\n")

    def load_meta(self) -> SessionMeta | None:
        if not self.meta_path.is_file():
            return None
        try:
            data = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        return SessionMeta.from_json(data)

    def set_last(self) -> None:
        target = last_path(self.workspace, home=self.home)
        current = read_last(self.workspace, home=self.home)
        if current == self.session_id:
            return
        atomic_write(target, self.session_id + "\n")


def read_last(workspace: Path, *, home: Path | None = None) -> str | None:
    path = last_path(workspace, home=home)
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


@dataclass
class SessionListItem:
    session_id: str
    title: str | None
    updated_at: float
    parent_id: str | None
    preview: str
    mtime: float


def session_has_user(workspace: Path, session_id: str, *, home: Path | None = None) -> bool:
    """有过用户任务才算可 --continue；只有 system 提示的空会话不算。"""
    root = home if home is not None else cyan_home()
    store = DiskStore(workspace=Path(workspace).resolve(), session_id=session_id, home=root)
    meta = store.load_meta()
    if meta is not None and meta.title:
        return True
    return any(event.type == USER for event in store.load_events())


def list_sessions(workspace: Path, *, home: Path | None = None) -> list[SessionListItem]:
    """扫描当前工作区 ``*.jsonl``。"""
    folder = project_dir(workspace, home=home)
    if not folder.is_dir():
        return []
    root = home if home is not None else cyan_home()
    items: list[SessionListItem] = []
    for jsonl in folder.glob("*.jsonl"):
        session_id = jsonl.stem
        if not session_id:
            continue
        store = DiskStore(workspace=Path(workspace).resolve(), session_id=session_id, home=root)
        meta = store.load_meta()
        mtime = jsonl.stat().st_mtime
        items.append(
            SessionListItem(
                session_id=session_id,
                title=meta.title if meta else None,
                updated_at=meta.updated_at if meta else mtime,
                parent_id=meta.parent_id if meta else None,
                preview=((meta.title if meta else None) or session_id)[:80],
                mtime=mtime,
            )
        )
    items.sort(key=lambda item: item.updated_at or item.mtime, reverse=True)
    return items


def resolve_session_id(workspace: Path, token: str, *, home: Path | None = None) -> str | None:
    """完整 uuid 或当前项目下唯一前缀。"""
    token = token.strip()
    if not token:
        return None
    items = list_sessions(workspace, home=home)
    exact = [item.session_id for item in items if item.session_id == token]
    if exact:
        return exact[0]
    prefix = [item.session_id for item in items if item.session_id.startswith(token)]
    if len(prefix) == 1:
        return prefix[0]
    return None


def latest_jsonl_id(workspace: Path, *, home: Path | None = None) -> str | None:
    folder = project_dir(workspace, home=home)
    if not folder.is_dir():
        return None
    files = [path for path in folder.glob("*.jsonl") if path.is_file()]
    if not files:
        return None
    newest = max(files, key=lambda path: path.stat().st_mtime)
    return newest.stem
