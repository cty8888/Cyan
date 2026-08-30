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
    max_glob_results: int = 100  # glob 按 mtime 返回的文件上限，触顶时提示收窄 pattern
    max_file_bytes: int = 2_000_000  # read_file / write_file 单次进内存上限
    max_bash_timeout_ms: int = 600_000  # bash timeout_ms 上限（10 分钟）
    max_process_output_chars: int = 1_000_000  # 子进程 stdout 入内存上限，防止 OOM
