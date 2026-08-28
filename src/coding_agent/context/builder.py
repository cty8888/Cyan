"""上下文装配：把 Message 历史与 ToolHistory 组合成发给模型的 wire 格式。

展示策略（例如工具结果用摘要还是全文）属于上下文层职责，不应下沉到
``ToolHistory`` 或 ``Message`` 内部。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..core.tool_history import RenderMode, ToolExecution, ToolHistory
from ..llm.types import Message, SystemMessage, ToolMessage


@dataclass
class ContextBuilder:
    """根据当前上下文需求，决定 Message / ToolHistory 如何呈现给模型。"""

    render_mode: RenderMode = "summary"

    def render_tool_result(self, execution: ToolExecution | None) -> str:
        """选择并渲染某次工具调用的输出文本。"""
        if execution is None or execution.result is None:
            return ""
        return execution.result.render(mode=self.render_mode)

    def build_messages(
        self,
        system_prompt: str,
        messages: list[Message],
        tool_history: ToolHistory,
    ) -> list[dict]:
        """装配 OpenAI 兼容的消息列表。"""
        payloads: list[dict] = [SystemMessage.of(system_prompt).to_api()]
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
