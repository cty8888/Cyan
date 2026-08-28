"""与厂商 SDK 解耦的内部消息结构。

Agent Loop 只依赖这里的数据类；换任何 OpenAI 兼容后端只需改 ``llm/`` 下的实现。

``Message`` 不再是一段简单字符串，而是由若干 ``Block``（内容块）组成：
一条消息里可能同时包含文本、工具调用、工具结果、文件引用、代码片段等不同来源、
不同结构的信息，Block 模型让这些信息各自独立、可组合，不必互相耦合进同一个字段。
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar


class Role(Enum):
    """消息发送者角色。用枚举代替裸字符串，避免任意值被注入。"""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class BlockType(Enum):
    """Block 的种类标签。"""

    TEXT = "text"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    FILE = "file"
    CODE = "code"


class ToolResultStatus(Enum):
    """工具执行结果的状态。"""

    OK = "ok"
    ERROR = "error"


class Block(ABC):
    """消息内容的最小不可再拆分单元，具体种类由子类声明。"""

    type: ClassVar[BlockType]


@dataclass
class TextBlock(Block):
    """自然语言文本：用户输入、assistant 回复、系统提示均属此类。"""

    type: ClassVar[BlockType] = BlockType.TEXT
    text: str


@dataclass
class ToolCallBlock(Block):
    """模型发起的一次工具调用。``arguments`` 保持原始 JSON 字符串，交由 parser 解析。"""

    type: ClassVar[BlockType] = BlockType.TOOL_CALL
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
class ToolResultBlock(Block):
    """一次工具调用的执行结果，回填给模型。"""

    type: ClassVar[BlockType] = BlockType.TOOL_RESULT
    tool_call_id: str
    content: str
    status: ToolResultStatus = ToolResultStatus.OK


@dataclass
class FileBlock(Block):
    """对某个文件的引用（用户或 Agent 提及了它），不携带文件内容本身。"""

    type: ClassVar[BlockType] = BlockType.FILE
    path: str
    start_line: int | None = None
    end_line: int | None = None


@dataclass
class CodeBlock(Block):
    """独立的代码片段，与 FileBlock 不同——它不必对应磁盘上的真实文件。"""

    type: ClassVar[BlockType] = BlockType.CODE
    language: str
    code: str


@dataclass
class Message:
    """一条会话消息：由角色 + 一组 Block 组成，不再是单一字符串。"""

    role: Role
    blocks: list[Block] = field(default_factory=list)

    @classmethod
    def system(cls, text: str) -> Message:
        return cls(role=Role.SYSTEM, blocks=[TextBlock(text=text)])

    @classmethod
    def user(cls, text: str) -> Message:
        return cls(role=Role.USER, blocks=[TextBlock(text=text)])

    @classmethod
    def assistant(cls, text: str | None = None, tool_calls: list[ToolCallBlock] | None = None) -> Message:
        blocks: list[Block] = []
        if text:
            blocks.append(TextBlock(text=text))
        blocks.extend(tool_calls or [])
        return cls(role=Role.ASSISTANT, blocks=blocks)

    @classmethod
    def tool(
        cls,
        tool_call_id: str,
        content: str,
        status: ToolResultStatus = ToolResultStatus.OK,
    ) -> Message:
        return cls(
            role=Role.TOOL,
            blocks=[ToolResultBlock(tool_call_id=tool_call_id, content=content, status=status)],
        )

    @property
    def text(self) -> str | None:
        """拼接消息里所有 TextBlock 的文本；没有文本块时返回 None。"""
        texts = [b.text for b in self.blocks if isinstance(b, TextBlock)]
        return "\n".join(texts) if texts else None

    @property
    def tool_calls(self) -> list[ToolCallBlock]:
        return [b for b in self.blocks if isinstance(b, ToolCallBlock)]

    @property
    def tool_result(self) -> ToolResultBlock | None:
        return next((b for b in self.blocks if isinstance(b, ToolResultBlock)), None)

    def to_api(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": self.role.value}

        if self.role is Role.TOOL:
            result = self.tool_result
            payload["content"] = result.content if result else ""
            if result:
                payload["tool_call_id"] = result.tool_call_id
            return payload

        tool_calls = self.tool_calls
        text = self.text
        # assistant 带 tool_calls 时 content 可以省略，但其余情况必须有字符串
        if text is not None or not tool_calls:
            payload["content"] = text or ""
        if tool_calls:
            payload["tool_calls"] = [tc.to_api() for tc in tool_calls]
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
