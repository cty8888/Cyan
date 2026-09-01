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
    """对某个文件的引用（用户 ``@path`` 提及了它），``content`` 是引用时刻的快照。

    快照而非实时路径：会话重放、压缩摘要都要看到「用户当时看到的内容」，
    不能因为文件之后被改写而让历史对话变得不可复现。
    """

    type: ClassVar[BlockType] = BlockType.FILE
    path: str
    content: str | None = None
    start_line: int | None = None
    end_line: int | None = None

    def render(self) -> str:
        """转成给模型看的纯文本形式，随 ``UserMessage.to_api()`` 一起拼进 content。"""
        header = f"[文件 {self.path}]"
        if self.content is None:
            return header
        return f"{header}\n```\n{self.content}\n```"


@dataclass
class CodeBlock(Block):
    """独立的代码片段，不必对应磁盘上的真实文件。"""

    type: ClassVar[BlockType] = BlockType.CODE
    language: str  # noqa  内容模型预留字段，目前还没有代码路径构造 CodeBlock
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
    def file_blocks(self) -> list[FileBlock]:
        """从 blocks 里滤出 ``@path`` 引用的文件（目前只有 UserMessage 会携带）。"""
        return [b for b in self.blocks if isinstance(b, FileBlock)]

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
        parts = []
        text = self.text
        if text:
            parts.append(text)
        parts.extend(block.render() for block in self.file_blocks)
        return {"role": self.role.value, "content": "\n\n".join(parts)}


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


@dataclass
class StreamChunk:
    """流式补全的一次增量：文本分片，或者某个 tool_call 参数 JSON 的分片。

    两者不会同时非空——OpenAI 兼容协议里一次 delta 只携带其中一种。完整响应
    （包括拼好的 tool_calls）由 generator 的 return 值给出，本类只用于向外展示
    「正在生成什么」的实时预览。

    ``tool_call_index`` 非 None 时表示这是一次 tool_call 分片：``tool_call_id``/
    ``tool_call_name`` 通常只在该调用的第一个分片里携带，之后为空字符串/None，
    调用方需要自己按 ``tool_call_index`` 记住。
    """

    text_delta: str = ""
    tool_call_index: int | None = None
    tool_call_id: str | None = None
    tool_call_name: str | None = None
    tool_call_arguments_delta: str = ""
