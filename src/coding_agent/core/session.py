"""会话状态。

MVP 阶段直接持有全量消息列表；Phase 3 的上下文压缩会在这层之上接管
``messages_for_request()``，Agent Loop 无需改动。
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..context.builder import ContextBuilder
from ..llm.types import Message, ToolResultStatus, Usage
from .tool_history import ToolExecution, ToolHistory, ToolResult

# 重复调用检测的观察窗口。只比较「上一次」调用会漏掉 A-B-A-B 这类交替循环，
# 因此改为统计最近若干次调用里同一指纹出现了几次。
RECENT_CALL_WINDOW = 8


@dataclass
class Session:
    system_prompt: str
    messages: list[Message] = field(default_factory=list)

    # Agent 执行工具的事实记录，与 Message 历史彻底分开
    tool_history: ToolHistory = field(default_factory=ToolHistory)

    # 上下文装配：决定工具结果如何呈现给模型
    context_builder: ContextBuilder = field(default_factory=ContextBuilder)

    # 用户在本会话中选择「始终允许」的工具
    always_allowed: set[str] = field(default_factory=set)

    total_usage: Usage = field(default_factory=Usage)
    llm_calls: int = 0
    tool_calls: int = 0
    consecutive_tool_failures: int = 0

    # 最近若干次工具调用的指纹，用于识别模型卡死在同一组动作上
    recent_calls: deque[str] = field(
        default_factory=lambda: deque(maxlen=RECENT_CALL_WINDOW), repr=False
    )

    # 本会话中已经完整满足过一次读取请求的文件（绝对路径），供 write_file/edit_file
    # 做"写之前必须先读"的前置检查——不要求读完整个文件，只要某次 read_file
    # 调用如实给出了它请求的内容（没被预算截断）即可
    read_files: set[Path] = field(default_factory=set, repr=False)

    # bash 工具的「当前目录」。每条命令都是独立新进程，没有持久 shell 可以延续 cd 的效果，
    # 所以由会话记住上一次命令结束后的目录；None 表示还没偏离过工作目录根
    bash_cwd: Path | None = None

    def add(self, message: Message) -> None:
        self.messages.append(message)

    def record_tool_execution(
        self,
        call_id: str,
        tool_name: str,
        arguments: str,
        ok: bool,
        content: str,
        *,
        error: str | None = None,
        duration: float = 0.0,
        started_at: float | None = None,
        finished_at: float | None = None,
    ) -> None:
        """把一次工具执行的事实写入 ``tool_history``。"""
        status = ToolResultStatus.OK if ok else ToolResultStatus.ERROR
        if not ok and error is None:
            error = content
        now = time.time()
        self.tool_history.record(
            ToolExecution(
                id=call_id,
                tool_name=tool_name,
                arguments=arguments,
                status=status,
                result=ToolResult(content=content),
                started_at=started_at if started_at is not None else now,
                finished_at=finished_at if finished_at is not None else now,
                duration=duration,
                error=error,
            )
        )

    def messages_for_request(self) -> list[dict[str, Any]]:
        """装配成 OpenAI 兼容的 wire 格式，展示策略由 ``context_builder`` 决定。"""
        return self.context_builder.build_messages(self.system_prompt, self.messages, self.tool_history)

    def record_usage(self, usage: Usage) -> None:
        self.total_usage = self.total_usage + usage
        self.llm_calls += 1

    def record_tool_outcome(self, ok: bool) -> None:
        self.tool_calls += 1
        self.consecutive_tool_failures = 0 if ok else self.consecutive_tool_failures + 1

    def record_call_fingerprint(self, name: str, args: dict[str, Any]) -> int:
        """记录一次工具调用，返回它在最近窗口内出现的次数（首次为 1）。"""
        payload = json.dumps({"name": name, "args": args}, sort_keys=True, ensure_ascii=False, default=str)
        fingerprint = hashlib.sha1(payload.encode("utf-8")).hexdigest()
        self.recent_calls.append(fingerprint)
        return self.recent_calls.count(fingerprint)

    def record_progress(self) -> None:
        """任务出现实质进展时清空窗口。

        「改完文件再跑一次测试」是正常迭代，不该被当成死循环；
        真正的死循环里不会有任何东西发生改变。
        """
        self.recent_calls.clear()

    def reset_repeat_tracking(self) -> None:
        self.recent_calls.clear()

    def mark_read(self, path: Path) -> None:
        """记录一次对 path 的读取已经如实满足了调用方的请求。"""
        self.read_files.add(path)

    def has_read(self, path: Path) -> bool:
        """path 是否在本会话中被满足过至少一次读取请求。"""
        return path in self.read_files

    def clear(self) -> None:
        self.messages.clear()
        self.tool_history.clear()
        self.total_usage = Usage()
        self.llm_calls = 0
        self.tool_calls = 0
        self.consecutive_tool_failures = 0
        self.reset_repeat_tracking()
        self.read_files.clear()
        self.bash_cwd = None

    def stats(self) -> dict[str, Any]:
        return {
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "prompt_tokens": self.total_usage.prompt_tokens,
            "completion_tokens": self.total_usage.completion_tokens,
            "total_tokens": self.total_usage.total_tokens,
            "messages": len(self.messages),
        }
