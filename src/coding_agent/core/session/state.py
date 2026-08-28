"""Agent 当前任务执行状态。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

RECENT_CALL_WINDOW = 8


@dataclass
class SessionState:
    current_task: str | None = None
    # TODO: 任务规划接入后使用 plan / current_step / variables
    plan: list[str] = field(default_factory=list)
    current_step: int = 0
    variables: dict[str, Any] = field(default_factory=dict)

    consecutive_tool_failures: int = 0
    recent_calls: deque[str] = field(
        default_factory=lambda: deque(maxlen=RECENT_CALL_WINDOW), repr=False
    )
