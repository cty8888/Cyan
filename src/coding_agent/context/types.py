"""上下文层的数据契约：工具结果展示模式与 token 预算。"""

from __future__ import annotations

from dataclasses import dataclass

from ..session.types import RenderMode


@dataclass
class ContextPolicy:
    """Runtime 上的上下文呈现策略，与模型调用参数（model / temperature）无关。"""

    tool_result_mode: RenderMode = "summary"
    max_context_tokens: int = 128_000
