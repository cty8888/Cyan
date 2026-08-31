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
    """一次任务结束的原因，供 CLI 展示与退出码判定。"""

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
class AssistantReplyDelta(AgentEvent):
    """模型流式输出的增量文本分片，用于实时展示（打字机效果）。

    同一轮里若干条 ``AssistantReplyDelta`` 之后必定紧跟一条携带完整文本的
    ``AssistantReply``——后者才是「这一轮文本已完整」的权威信号，
    ``final_text``、Auto Memory、压缩等下游逻辑仍只看 ``AssistantReply``。
    """

    text: str


@dataclass
class AssistantReply(AgentEvent):
    """模型给出的可见文本（不含 tool_calls，那些走 ToolStarted）。"""

    text: str
    

@dataclass
class ToolCallDelta(AgentEvent):
    """流式过程中一次工具调用参数 JSON 的增量分片，用于 CLI 实时预览。

    比如 ``write_file``/``edit_file`` 可以边生成边把文件内容"typing" 出来，
    跟 Claude Code 靠 Anthropic 的 fine-grained tool streaming 做的效果一样。

    ``index`` 是本轮响应内的顺序号（模型可能一次发起多个工具调用）；
    ``call_id``/``name`` 通常只在该调用的第一个分片里携带，之后为 None，
    需要消费方自己按 ``index`` 记住。真正执行仍然要等完整 JSON 拼好、
    经过 ``parse_tool_arguments`` 解析之后才会发起（见 ``ToolStarted``）。
    """

    index: int
    call_id: str | None
    name: str | None
    arguments_delta: str


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
