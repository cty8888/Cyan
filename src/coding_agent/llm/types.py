"""与厂商 SDK 解耦的内部消息结构。

Agent Loop 只依赖这里的数据类；换任何 OpenAI 兼容后端只需改 ``llm/`` 下的实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    """模型发起的一次工具调用。``arguments`` 保持原始 JSON 字符串，交由 parser 解析。"""

    id: str
    name: str
    arguments: str = ""

    def to_api(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments or "{}"},
        }


@dataclass
class Message:
    role: str
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None

    @classmethod
    def system(cls, content: str) -> Message:
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> Message:
        return cls(role="user", content=content)

    @classmethod
    def assistant(cls, content: str | None = None, tool_calls: list[ToolCall] | None = None) -> Message:
        return cls(role="assistant", content=content, tool_calls=tool_calls or [])

    @classmethod
    def tool(cls, tool_call_id: str, content: str) -> Message:
        return cls(role="tool", content=content, tool_call_id=tool_call_id)

    def to_api(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": self.role}
        # assistant 带 tool_calls 时 content 可以为 None，但其余角色必须有字符串
        if self.content is not None or not self.tool_calls:
            payload["content"] = self.content or ""
        if self.tool_calls:
            payload["tool_calls"] = [tc.to_api() for tc in self.tool_calls]
        if self.tool_call_id:
            payload["tool_call_id"] = self.tool_call_id
        return payload


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
            self.total_tokens + other.total_tokens,
        )


@dataclass
class LLMResponse:
    message: Message
    finish_reason: str = "stop"
    usage: Usage = field(default_factory=Usage)
