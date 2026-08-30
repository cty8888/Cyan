"""会话事件：内存表与 jsonl 共用的外壳。"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

EVENT_VERSION = 1

SESSION_STARTED = "session_started"
USER = "user"
CONTINUE = "continue"
ASSISTANT = "assistant"
TOOL_RESULT = "tool_result"
SUMMARY = "summary"
COMPACT = "compact"
CHECKPOINT = "checkpoint"
FILE_OP = "file_op"
BRANCH_FORKED = "branch_forked"

# 重放时组成对话/工具视图的事件（summary 由 compact 插入，不按文件位置 emit）
SOURCE_TYPES = frozenset({SESSION_STARTED, USER, CONTINUE, ASSISTANT, TOOL_RESULT})
# fork 时拷贝的源事件（不含 compact/summary）
FORK_COPY_TYPES = frozenset({SESSION_STARTED, USER, CONTINUE, ASSISTANT, TOOL_RESULT, CHECKPOINT, FILE_OP})

COMPACT_REASON_AUTO = "auto"
COMPACT_REASON_MANUAL = "manual"
COMPACT_REASON_EMERGENCY = "emergency"
COMPACT_REASON_SUMMARIZE_UP = "summarize_up"
COMPACT_REASON_SUMMARIZE_FROM = "summarize_from"


def new_event_id() -> str:
    return "evt_" + uuid.uuid4().hex


def _now() -> float:
    return time.time()


@dataclass
class SessionEvent:
    """一条会话事件。``payload`` 按 ``type`` 解释。"""

    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=new_event_id)
    ts: float = field(default_factory=_now)
    parent_id: str | None = None
    v: int = EVENT_VERSION

    def to_json(self) -> dict[str, Any]:
        return {
            "v": self.v,
            "id": self.id,
            "type": self.type,
            "ts": self.ts,
            "parent_id": self.parent_id,
            "payload": self.payload,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SessionEvent:
        return cls(
            v=int(data.get("v") or EVENT_VERSION),
            id=str(data.get("id") or new_event_id()),
            type=str(data.get("type") or ""),
            ts=float(data.get("ts") or 0.0),
            parent_id=data.get("parent_id"),
            payload=dict(data.get("payload") or {}),
        )
