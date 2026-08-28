"""Session 自身元信息，不参与 Agent 推理。"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field


def _new_session_id() -> str:
    return str(uuid.uuid4())


def _now() -> float:
    return time.time()


@dataclass
class SessionMetadata:
    id: str = field(default_factory=_new_session_id)
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)
    title: str | None = None

    @classmethod
    def create(cls, title: str | None = None) -> SessionMetadata:
        """创建一组新的会话元信息。"""
        return cls(title=title)
