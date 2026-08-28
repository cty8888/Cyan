"""Session 自身元信息，不参与 Agent 推理。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SessionMetadata:
    # TODO: 任务开始时自动填充 title（取自首条 user 消息或 current_task）
    id: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    title: str | None = None
