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
    max_tokens: int = 8_192  # 单次补全上限；不设的话厂商默认过小，容易 finish_reason=length
    request_timeout: float = 180.0  # 单次 HTTP 请求超时（秒）
    max_retries: int = 3  # 可重试错误的额外尝试次数，不含首次
