"""消息、内容块与模型响应的内部类型。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar


class Role(Enum):
    """消息发送者角色。"""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class BlockType(Enum):
    """内容块的种类标签。"""

    TEXT = "text"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    FILE = "file"
    CODE = "code"


class Block(ABC):
    """消息内容的最小不可再拆分单元。"""

    type: ClassVar[BlockType]


@dataclass
class TextBlock(Block):
    """自然语言文本：用户输入、assistant 回复、系统提示均属此类。"""

    type: ClassVar[BlockType] = BlockType.TEXT
    text: str


@dataclass
class ToolCallBlock(Block):
    """模型发起的一次工具调用。``arguments`` 保持原始 JSON 字符串，由 parser 解析。"""

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
    """对一次工具调用结果的引用，只留 call id。

    真正的输出内容、执行状态属于 Agent 的事实记录（见 ``session.types.ToolHistory``），
    不属于 Message——这里只负责「指向哪一次调用」。
    """

    type: ClassVar[BlockType] = BlockType.TOOL_RESULT
    tool_call_id: str


@dataclass
class FileBlock(Block):
    """对某个文件的引用（用户或 Agent 提及了它），不携带文件内容本身。"""

    type: ClassVar[BlockType] = BlockType.FILE
    path: str
    start_line: int | None = None
    end_line: int | None = None


@dataclass
class CodeBlock(Block):
    """独立的代码片段，不必对应磁盘上的真实文件。"""

    type: ClassVar[BlockType] = BlockType.CODE
    language: str
    code: str


@dataclass
class Message(ABC):
    """所有消息角色的公共基类：``role`` + 一组 ``blocks``。

    子类只约束「允许出现哪些 Block」以及「怎么转成 API payload」，
    不在 Message 层再开业务字段。``id`` 是事件日志里的身份，不是业务载荷。
    """

    role: ClassVar[Role]
    blocks: list[Block] = field(default_factory=list)
    id: str | None = None

    @property
    def text(self) -> str | None:
        """拼接消息里所有 TextBlock 的文本；没有文本块时返回 None。"""
        texts = [b.text for b in self.blocks if isinstance(b, TextBlock)]
        return "\n".join(texts) if texts else None

    @property
    def tool_calls(self) -> list[ToolCallBlock]:
        """从 blocks 里滤出模型发起的工具调用。"""
        return [b for b in self.blocks if isinstance(b, ToolCallBlock)]

    @property
    def tool_result(self) -> ToolResultBlock | None:
        return next((b for b in self.blocks if isinstance(b, ToolResultBlock)), None)

    @abstractmethod
    def to_api(self) -> dict[str, Any]:
        """转换成 OpenAI 兼容的 wire 格式，供 ``LLMClient`` 发请求用。"""


@dataclass
class SystemMessage(Message):
    """系统提示词：只包含一个 TextBlock。"""

    role: ClassVar[Role] = Role.SYSTEM

    @classmethod
    def of(cls, text: str) -> SystemMessage:
        return cls(blocks=[TextBlock(text=text)])

    def to_api(self) -> dict[str, Any]:
        return {"role": self.role.value, "content": self.text or ""}


@dataclass
class UserMessage(Message):
    """用户输入，可能引用文件、贴代码，因此保留完整 Block 能力。"""

    role: ClassVar[Role] = Role.USER

    @classmethod
    def of(cls, text: str) -> UserMessage:
        return cls(blocks=[TextBlock(text=text)])

    def to_api(self) -> dict[str, Any]:
        return {"role": self.role.value, "content": self.text or ""}


@dataclass
class SummaryMessage(Message):
    """被压缩掉的对话区间收成的摘要。wire 仍用 user 角色（厂商没有 summary 角色）。

    与 ``UserMessage`` 平级，不算用户任务轮次；再压缩时会落入被压缩段。
    """

    role: ClassVar[Role] = Role.USER

    @classmethod
    def of(cls, text: str) -> SummaryMessage:
        return cls(blocks=[TextBlock(text=text)])

    def to_api(self) -> dict[str, Any]:
        return {"role": self.role.value, "content": self.text or ""}


@dataclass
class ContinueMessage(Message):
    """模型输出被截断时插入的续写指令。wire 仍用 user 角色。

    不是用户任务：压缩时不能当成当前问题原文保留。
    """

    role: ClassVar[Role] = Role.USER

    @classmethod
    def of(cls, text: str) -> ContinueMessage:
        return cls(blocks=[TextBlock(text=text)])

    def to_api(self) -> dict[str, Any]:
        return {"role": self.role.value, "content": self.text or ""}


@dataclass
class AssistantMessage(Message):
    """模型回复：TextBlock + 若干 ToolCallBlock。"""

    role: ClassVar[Role] = Role.ASSISTANT

    @classmethod
    def of(cls, text: str | None = None, tool_calls: list[ToolCallBlock] | None = None) -> AssistantMessage:
        blocks: list[Block] = []
        if text:
            blocks.append(TextBlock(text=text))
        blocks.extend(tool_calls or [])
        return cls(blocks=blocks)

    def to_api(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": self.role.value}
        tool_calls = self.tool_calls
        text = self.text
        # 带 tool_calls 时 content 可以省略，但其余情况必须有字符串
        if text is not None or not tool_calls:
            payload["content"] = text or ""
        if tool_calls:
            payload["tool_calls"] = [tc.to_api() for tc in tool_calls]
        return payload


@dataclass
class ToolMessage(Message):
    """一次工具调用的结果回复：只包含一个 ToolResultBlock（只有 call id）。

    ``content`` 由 ``ContextBuilder`` 根据 ``ToolHistory`` 查到的记录渲染后传入，
    不在 ``Message`` 层自行反查或决定展示策略。
    """

    role: ClassVar[Role] = Role.TOOL

    @classmethod
    def of(cls, tool_call_id: str) -> ToolMessage:
        return cls(blocks=[ToolResultBlock(tool_call_id=tool_call_id)])

    def to_api(self, *, content: str = "") -> dict[str, Any]:
        result = self.tool_result
        call_id = result.tool_call_id if result else ""
        return {
            "role": self.role.value,
            "content": content,
            "tool_call_id": call_id,
        }


@dataclass
class Usage:
    """一次模型调用的 token 用量，可累加。"""

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
    """模型响应总是携带一条 assistant 消息。"""

    message: AssistantMessage
    finish_reason: str = "stop"
    usage: Usage = field(default_factory=Usage)
