"""DeepSeek（OpenAI 兼容协议）客户端实现。"""

from __future__ import annotations

import random
import time
from typing import Any, Callable

import openai
from openai import OpenAI

from ..config import Config
from ..errors import (
    LLMAuthError,
    LLMConnectionError,
    LLMError,
    LLMRateLimitError,
    LLMResponseError,
)
from ..logutil import get_logger
from .base import LLMClient
from .parser import parse_completion
from .types import LLMResponse, Message

logger = get_logger("llm")


class DeepSeekClient(LLMClient):
    """带指数退避重试的对话客户端。

    ``on_retry`` 用于把重试情况透传到 rich 界面，避免长时间静默等待。
    """

    def __init__(self, config: Config, on_retry: Callable[[int, float, str], None] | None = None):
        self.model = config.model
        self._config = config
        self._on_retry = on_retry
        self._client = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.request_timeout,
            max_retries=0,  # 重试逻辑由本类自己掌控，便于向用户反馈
        )

    def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_api() for m in messages],
            "temperature": self._config.temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        last_error: LLMError | None = None
        for attempt in range(self._config.max_retries + 1):
            try:
                return parse_completion(self._client.chat.completions.create(**payload))
            except LLMResponseError:
                raise
            except Exception as exc:  # noqa: BLE001 - 统一映射为内部异常
                error = _map_exception(exc)
                if not error.retryable or attempt == self._config.max_retries:
                    raise error from exc
                last_error = error
                delay = _backoff_delay(attempt)
                logger.warning("模型调用失败（%s），%.1fs 后第 %s 次重试", error, delay, attempt + 1)
                if self._on_retry:
                    self._on_retry(attempt + 1, delay, str(error))
                time.sleep(delay)

        raise last_error or LLMError("模型调用失败")


def _backoff_delay(attempt: int) -> float:
    """指数退避 + 抖动，避免多次重试同时打到服务端。"""
    return min(2.0**attempt, 8.0) + random.uniform(0, 0.5)


def _map_exception(exc: Exception) -> LLMError:
    if isinstance(exc, openai.AuthenticationError):
        return LLMAuthError("API Key 无效或已过期，请检查 .env 中的 DEEPSEEK_API_KEY")
    if isinstance(exc, openai.PermissionDeniedError):
        return LLMAuthError(f"没有访问该模型的权限：{exc}")
    if isinstance(exc, openai.RateLimitError):
        return LLMRateLimitError("触发限流，稍后重试")
    if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError)):
        return LLMConnectionError(f"网络连接失败：{exc}")
    if isinstance(exc, openai.InternalServerError):
        return LLMConnectionError(f"模型服务端错误：{exc}")
    if isinstance(exc, openai.BadRequestError):
        return LLMError(f"请求非法：{exc}")
    if isinstance(exc, openai.APIStatusError):
        return LLMError(f"模型服务返回错误 {exc.status_code}：{exc}")
    return LLMConnectionError(f"调用模型时发生未预期错误：{exc}")
