"""模型客户端抽象。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from .types import LLMResponse


class LLMClient(ABC):
    """任何后端只要实现 ``chat`` 即可接入 Agent Loop。

    ``messages`` 是已经渲染好的 OpenAI 兼容 wire 格式（由 ``Session`` 负责把
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
