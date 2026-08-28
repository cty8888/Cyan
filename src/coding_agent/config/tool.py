"""工具层运行时限额。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ToolConfig:
    """工具执行时的截断与列表上限，与 LLM / Agent 配置无关。"""

    max_tool_output_chars: int = 30_000
    max_file_read_chars: int = 60_000
    max_dir_entries: int = 400
