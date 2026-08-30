"""Agent Loop 的终止与防护上限。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LoopLimits:
    max_iterations: int = 30  # 单次任务最多「模型 ↔ 工具」轮次
    max_consecutive_tool_failures: int = 3  # 连续失败这么多次就停
    max_repeated_calls: int = 3  # 同工具同参数连续出现这么多次视为死循环
