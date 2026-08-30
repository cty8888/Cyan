"""工具执行的截断与列表上限。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ToolLimits:
    max_tool_output_chars: int = 30_000  # bash 等工具回喂模型的输出上限
    max_file_read_chars: int = 60_000  # 单次 read_file 的字符预算
    max_dir_entries: int = 400  # list_dir 树形列表的条目上限
