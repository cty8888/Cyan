"""工具执行历史：Agent 执行工具的事实记录，独立于 Message / Block 模型。

``ToolHistory`` 只负责保存与查询事实（``record`` / ``get`` / ``remove``），不承担「如何展示给模型」
的职责——那是 ``context.builder.ContextBuilder`` 根据当前上下文需求决定的事。

``ToolResult`` 只保存结果数据并提供基础渲染能力；压缩策略（生成 summary、保存原文、删除 content）
属于 ``CompressionManager``，展示模式选择属于 ``ContextBuilder``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ..llm.types import ToolResultStatus

RenderMode = Literal["summary", "full"]


@dataclass
class ToolResult:
    """一次工具调用的输出数据。

    TODO: 重命名以避免与 tools.base.ToolRunResult 混淆 (如 StoredToolOutput / ToolOutputRecord).
    """

    content: str | None = None
    summary: str | None = None
    ref: str | None = None

    @property
    def has_summary(self) -> bool:
        return self.summary is not None

    @property
    def content_removed(self) -> bool:
        return self.content is None and self.ref is not None

    def render(self, mode: RenderMode = "summary") -> str:
        """按指定模式渲染给模型看的文本。

        ``mode="summary"``：优先返回 ``summary``，没有则返回 ``content``。
        ``mode="full"``：若 ``content`` 存在则返回原文；若原文已删除但有 ``ref`` 则返回引用提示；
        否则退回 ``summary``。
        """
        if mode == "summary":
            if self.summary is not None:
                return self.summary
            return self.content or ""

        if self.content is not None:
            return self.content
        if self.ref is not None:
            hint = f"[完整结果已移除，存储引用：{self.ref}]"
            return f"{self.summary}\n{hint}" if self.summary else hint
        return self.summary or ""


@dataclass
class ToolExecution:
    """一次工具调用的完整事实记录。"""

    id: str = ""
    tool_name: str = ""
    arguments: str = ""
    status: ToolResultStatus = ToolResultStatus.RUNNING
    result: ToolResult | None = None
    started_at: float = 0.0
    finished_at: float = 0.0
    duration: float = 0.0
    error: str | None = None


@dataclass
class ToolHistory:
    """会话内所有工具执行记录，按 call id 索引。"""

    executions: dict[str, ToolExecution] = field(default_factory=dict)

    def record(self, execution: ToolExecution) -> None:
        self.executions[execution.id] = execution

    def get(self, call_id: str) -> ToolExecution | None:
        return self.executions.get(call_id)

    def remove(self, call_id: str) -> ToolExecution | None:
        """移除指定 call id 的记录；存在则返回被移除的 ``ToolExecution``，否则 ``None``。"""
        return self.executions.pop(call_id, None)

    def clear(self) -> None:
        self.executions.clear()
