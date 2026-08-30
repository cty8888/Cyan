"""上下文层的数据契约：工具结果展示模式与 token 预算。"""

from __future__ import annotations

from dataclasses import dataclass

from ..session.types import RenderMode


@dataclass
class ContextPolicy:
    """装配层策略：工具结果在发给模型时用摘要还是原文。

    不决定何时压缩、压多少——那是 Runtime 上的 ``CompactPolicy``。
    发给模型的内容来自 Session 的 messages + tool_history。
    """

    tool_result_mode: RenderMode = "summary"
