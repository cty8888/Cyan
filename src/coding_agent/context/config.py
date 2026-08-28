"""上下文装配相关配置。"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.tool_history import RenderMode


@dataclass
class ContextConfig:
    """控制上下文如何呈现给模型（展示模式、token 预算等）。

    属于上下文层配置，与模型调用参数（model / temperature）无关。
    Session 持有一份实例作为数据的一部分。
    """

    tool_result_mode: RenderMode = "summary"
    max_context_tokens: int = 128_000
