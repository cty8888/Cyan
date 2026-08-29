"""Agent Loop 向外发出的事件。

内核通过 yield 事件与外界通信，不直接做任何输入输出，
因此换成 TUI、Web 或测试桩都不需要改动 ``core/loop.py`` / ``core/runtime.py``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Generator

from ..security.types import ApprovalDecision, ApprovalRequest
from ..tools.types import ToolRunResult


class StopReason(Enum):
    COMPLETED = "completed"
    MAX_ITERATIONS = "max_iterations"
    TOOL_FAILURES = "tool_failures"
    REPEATED_CALLS = "repeated_calls"
    USER_ABORT = "user_abort"
    FATAL_ERROR = "fatal_error"


@dataclass
class AgentEvent:
    """所有事件的基类。"""


@dataclass
class TaskStarted(AgentEvent):
    task: str


@dataclass
class Thinking(AgentEvent):
    """正在等待模型响应。"""

    iteration: int


@dataclass
class AssistantReply(AgentEvent):
    """模型给出的可见文本（不含 tool_calls，那些走 ToolStarted）。"""

    text: str


@dataclass
class ApprovalRequired(AgentEvent):
    """需要外部回传一个 ``ApprovalDecision``。"""

    request: ApprovalRequest


@dataclass
class ToolStarted(AgentEvent):
    call_id: str
    name: str
    args: dict[str, Any]


@dataclass
class ToolFinished(AgentEvent):
    call_id: str
    name: str
    result: ToolRunResult
    duration: float


@dataclass
class Notice(AgentEvent):
    """提示信息：重试、降级、策略拦截等。"""

    message: str
    level: str = "info"


@dataclass
class TaskFinished(AgentEvent):
    reason: StopReason
    final_text: str = ""
    stats: dict[str, Any] = field(default_factory=dict)


AgentStream = Generator[AgentEvent, ApprovalDecision | None, None]
