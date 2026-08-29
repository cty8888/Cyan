"""LLM 调用参数：模型、地址、超时与重试。"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"


@dataclass
class LLMSettings:
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    temperature: float = 0.0
    request_timeout: float = 180.0
    max_retries: int = 3
