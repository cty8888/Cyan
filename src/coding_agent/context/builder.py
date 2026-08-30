"""上下文装配：把 Message 历史与 ToolHistory 组合成发给模型的 wire 格式。"""

from __future__ import annotations

from dataclasses import dataclass

from ..llm.types import Message, ToolMessage
from ..session.types import ToolExecution, ToolHistory
from .types import ContextPolicy


@dataclass
class ContextBuilder:
    """把 Session 的 messages + tool_history 译成 API wire。

    只负责装配，不决定何时压缩，也不做滑动窗口。压缩策略在 Runtime 的 ``CompactPolicy``。
    单条工具结果按 ``max_tool_result_chars`` 截尾，不写回 ``tool_history``。
    """

    max_tool_result_chars: int = 30_000

    @classmethod
    def from_policy(cls, policy: ContextPolicy) -> ContextBuilder:
        """从 Runtime 上的上下文策略构造 builder。"""
        return cls(max_tool_result_chars=policy.max_tool_result_chars)

    def render_tool_result(self, execution: ToolExecution | None) -> str:
        """取出一次工具执行的正文，必要时截尾。"""
        if execution is None or execution.result is None:
            return ""
        return _truncate_tail(execution.result.content or "", self.max_tool_result_chars)

    def build_messages(
        self,
        messages: list[Message],
        tool_history: ToolHistory,
    ) -> list[dict]:
        """ToolMessage 只存 call id，正文从 tool_history 查出后再填进 API payload。"""
        payloads: list[dict] = []
        for message in messages:
            if isinstance(message, ToolMessage):
                block = message.tool_result
                call_id = block.tool_call_id if block else ""
                execution = tool_history.get(call_id)
                content = self.render_tool_result(execution)
                payloads.append(message.to_api(content=content))
            else:
                payloads.append(message.to_api())
        return payloads


def _truncate_tail(text: str, limit: int) -> str:
    """超过上限时保留开头，并加上截断标记。``limit <= 0`` 表示不截。"""
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"
