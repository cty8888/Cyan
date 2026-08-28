"""Agent 执行模式。

Layer 2：在工具固有风险之上，决定默认放行策略。
"""

from __future__ import annotations

from enum import Enum


class ExecutionMode(Enum):
    ASK = "ask"
    AGENT = "agent"
    YOLO = "yolo"

    @classmethod
    def parse(cls, raw: str) -> ExecutionMode:
        try:
            return cls(str(raw).strip().lower())
        except ValueError as exc:
            raise ValueError(f"未知执行模式 {raw!r}，可选：ask / agent / yolo") from exc


MODE_LABELS = {
    ExecutionMode.ASK: "Ask（只读）",
    ExecutionMode.AGENT: "Agent（默认）",
    ExecutionMode.YOLO: "YOLO（宽松）",
}
