"""上下文装配：把 Message 历史与 ToolHistory 组合成发给模型的 wire 格式。"""

from __future__ import annotations

from dataclasses import dataclass

from .config import ContextConfig
from ..core.tool_history import RenderMode, ToolExecution, ToolHistory
from ..llm.types import Message, ToolMessage


@dataclass
class ContextBuilder:
    """根据当前上下文需求，决定 Message / ToolHistory 如何呈现给模型。

    TODO: 接入 CompressionManager——按 token 预算决定哪些 ToolExecution 压缩、render_mode 切换。
    TODO: 读取 session.state / workspace 参与上下文裁剪（Phase 3）。
    """

    render_mode: RenderMode = "summary"

    @classmethod
    def from_config(cls, config: ContextConfig) -> ContextBuilder:
        return cls(render_mode=config.tool_result_mode)

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
