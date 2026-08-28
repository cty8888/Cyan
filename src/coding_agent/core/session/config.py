"""Session 级配置快照。

TODO: 统一配置来源——明确 Session.config 与会话外 Config 的分工；
      运行中变更（如 /mode、tool_result_mode）应写回 Session.config 并由 Runtime 同步。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..tool_history import RenderMode

if TYPE_CHECKING:
    from ...config import Config


@dataclass
class SessionConfig:
    model: str = ""
    max_context_tokens: int = 128_000
    tool_result_mode: RenderMode = "summary"
    temperature: float = 0.0
    system_prompt: str = ""

    @classmethod
    def from_app_config(cls, config: Config, system_prompt: str) -> SessionConfig:
        return cls(
            model=config.model,
            temperature=config.temperature,
            system_prompt=system_prompt,
        )
