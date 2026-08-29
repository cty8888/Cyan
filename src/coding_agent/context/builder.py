"""上下文装配：把 Message 历史与 ToolHistory 组合成发给模型的 wire 格式。"""

from __future__ import annotations

from dataclasses import dataclass

from ..llm.types import Message, ToolMessage
from ..session.types import RenderMode, ToolExecution, ToolHistory
from .types import ContextPolicy


@dataclass
class ContextBuilder:
    """根据当前上下文需求，决定 Message / ToolHistory 如何呈现给模型。

    压缩与 token 预算裁剪尚未接入，目前按 ``render_mode`` 渲染工具结果。
    """

    render_mode: RenderMode = "summary"

    @classmethod
    def from_policy(cls, policy: ContextPolicy) -> ContextBuilder:
        return cls(render_mode=policy.tool_result_mode)

    def render_tool_result(self, execution: ToolExecution | None) -> str:
        if execution is None or execution.result is None:
            return ""
        return execution.result.render(mode=self.render_mode)

    def build_messages(
        self,
        messages: list[Message],
        tool_history: ToolHistory,
    ) -> list[dict]:
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
