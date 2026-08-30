"""上下文层的数据契约：装配期截断。"""

from __future__ import annotations

from dataclasses import dataclass

from ..settings.tools import DEFAULT_TOOL_RESULT_CHARS


@dataclass
class ContextPolicy:
    """装配层策略：发给模型时单条工具结果最长多少。

    不决定何时压缩、压多少——那是 Runtime 上的 ``CompactPolicy``。
    不改 Session：截断只发生在发给模型的 wire 上。
    ``max_tool_result_chars <= 0`` 表示不截断。
    默认与 ``ToolLimits.max_file_read_chars`` 对齐，避免模型看到的比工具声称的少。
    """

    max_tool_result_chars: int = DEFAULT_TOOL_RESULT_CHARS
