"""与厂商 SDK 解耦的内部消息结构。

Agent Loop 只依赖这里的数据类；换任何 OpenAI 兼容后端只需改 ``llm/`` 下的实现。

两个独立的继承体系：

- ``Block``：消息内容的最小单元（文本 / 工具调用 / 工具结果 / 文件引用 / 代码片段），
  真正的信息都装在这里，``Block`` 不知道、也不关心谁持有它。
- ``Message``：按角色拆成 ``SystemMessage`` / ``UserMessage`` / ``AssistantMessage`` /
  ``ToolMessage``，每一种都只是「``role`` + 一组 ``blocks``」，不直接持有任何业务字段——
  哪怕是 ``ToolMessage`` 的 ``tool_call_id``，也是通过它持有的 ``ToolResultBlock`` 表达，
  而不是在 ``Message`` 上另开字段。

``ToolResultBlock`` 本身也只留一个 call id：一次工具调用真正的输出内容、是否成功、
是否被压缩过，属于 Agent 执行工具的事实记录，不属于 Message——那是
``core.tool_history.ToolHistory`` 管的事。渲染给模型时由 ``context.builder.ContextBuilder``
反查 ``ToolHistory`` 并按上下文策略选择展示 ``content`` 还是 ``summary``。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar


class Role(Enum):
    """消息发送者角色"""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class BlockType(Enum):
    """Block 的种类标签"""

    TEXT = "text"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    FILE = "file"
    CODE = "code"


class ToolResultStatus(Enum):
    """工具执行结果的状态"""

    RUNNING = "running"
    OK = "ok"
    ERROR = "error"


class Block(ABC):
    """消息内容的最小不可再拆分单元"""

    type: ClassVar[BlockType]


@dataclass
class TextBlock(Block):
    """自然语言文本：用户输入、assistant 回复、系统提示均属此类"""

    type: ClassVar[BlockType] = BlockType.TEXT
    text: str


@dataclass
class ToolCallBlock(Block):
    """模型发起的一次工具调用,``arguments`` 保持原始 JSON 字符串，由 parser 解析"""

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

    真正的输出内容、执行状态属于 Agent 的事实记录（见 ``core.tool_history``），
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
    """独立的代码片段，与 FileBlock 不同——它不必对应磁盘上的真实文件。"""

    type: ClassVar[BlockType] = BlockType.CODE
    language: str
    code: str


@dataclass
class Message(ABC):
    """所有消息角色的公共基类：``role`` + 一组 ``blocks``

    子类之间的差异只体现在「允许出现哪些 Block」以及「怎么转成 API payload」上，
    不应该在 Message 层再额外开业务字段——否则又会退化回原来那种什么都能塞的大类。
    """

    role: ClassVar[Role]
    blocks: list[Block] = field(default_factory=list)

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
