"""模型客户端抽象。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from .types import LLMResponse, Message


class LLMClient(ABC):
    """任何后端只要实现 ``chat`` 即可接入 Agent Loop。"""

    model: str

    @abstractmethod
    def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        """发起一次对话补全，返回解析后的响应。

        实现需要负责重试，并把厂商异常映射为 ``errors.LLMError`` 家族。
        """
