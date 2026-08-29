"""Agent Loop 的终止与防护上限。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LoopLimits:
    max_iterations: int = 30
    max_consecutive_tool_failures: int = 3
    max_repeated_calls: int = 3
