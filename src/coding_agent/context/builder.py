"""上下文装配：把 Message 历史与 ToolHistory 组合成发给模型的 wire 格式。"""

from __future__ import annotations

from dataclasses import dataclass

from ..llm.types import Message, ToolMessage
from ..session.types import RenderMode, ToolExecution, ToolHistory
from .types import ContextPolicy


@dataclass
class ContextBuilder:
    """根据当前上下文需求，决定 Message / ToolHistory 如何呈现给模型。

    只负责装配，不决定何时压缩。压缩策略在 Runtime 的 ``CompactPolicy``。
    """

    render_mode: RenderMode = "summary"

    @classmethod
    def from_policy(cls, policy: ContextPolicy) -> ContextBuilder:
        """从 Runtime 上的上下文策略构造 builder。"""
        return cls(render_mode=policy.tool_result_mode)

    def render_tool_result(self, execution: ToolExecution | None) -> str:
        """按当前 render_mode 取出一次工具执行给模型看的文本。"""
        if execution is None or execution.result is None:
            return ""
        return execution.result.render(mode=self.render_mode)

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
