"""工具执行的截断与列表上限。"""

from __future__ import annotations

from dataclasses import dataclass

# 工具产出与组窗截断共用同一上限，避免 read_file 声称读完、模型却只看到前半段。
DEFAULT_TOOL_RESULT_CHARS = 30_000


@dataclass
class ToolLimits:
    max_tool_output_chars: int = DEFAULT_TOOL_RESULT_CHARS  # bash 等工具回喂模型的输出上限
    # 必须 <= ContextPolicy.max_tool_result_chars，否则组窗会再截一刀。
    max_file_read_chars: int = DEFAULT_TOOL_RESULT_CHARS
    max_dir_entries: int = 400  # list_dir 树形列表的条目上限
