"""Agent 使用统计。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SessionUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
