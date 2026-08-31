"""模型客户端抽象。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Generator

from .types import LLMResponse, StreamChunk


class LLMClient(ABC):
    """任何后端只要实现 ``chat`` 即可接入 Agent Loop。

    ``messages`` 是已经渲染好的 OpenAI 兼容 wire 格式（由 ``ContextBuilder`` 把
    ``Message`` 结合 ``ToolHistory`` 转换而成），``LLMClient`` 不需要认识
    ``Message`` 这个内部类型。
    """

    model: str

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        """发起一次对话补全，返回解析后的响应。

        实现需要负责重试，并把厂商异常映射为 ``errors.LLMError`` 家族。
        """

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> Generator[StreamChunk, None, LLMResponse]:
        """流式发起一次对话补全：边生成边 yield 增量，结束后 return 完整响应。

        默认退化成一次性 ``chat()``，把整段文本、以及每个 tool_call 的完整参数
        当成「一次性到位」的分片 yield 出去——任何只实现了 ``chat()`` 的子类
        （包括测试里的 FakeLLM）都能直接工作，CLI 侧的流式渲染逻辑不需要区分
        「真流式」还是「退化成一次性」。真正支持 SSE 的后端（如
        ``DeepSeekClient``）应当覆写本方法做逐分片输出。
        """
        response = self.chat(messages, tools=tools)
        if response.message.text:
            yield StreamChunk(text_delta=response.message.text)
        for index, call in enumerate(response.message.tool_calls):
            yield StreamChunk(
                tool_call_index=index,
                tool_call_id=call.id,
                tool_call_name=call.name,
                tool_call_arguments_delta=call.arguments,
            )
        return response
