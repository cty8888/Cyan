"""一次运行的全部可调参数入口。

优先级：命令行参数 > 环境变量 > 内置默认值。
各域设置分见 ``LLMSettings`` / ``LoopLimits`` / ``ToolLimits`` / ``CliSettings`` / ``CompactPolicy``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import ConfigError
from .cli import CliSettings
from .compact import CompactPolicy
from .llm import LLMSettings
from .loop import LoopLimits
from .tools import ToolLimits


@dataclass
class AgentSettings:
    """一次运行的工作区，以及按职责分开的各域设置。"""

    workspace: Path
    llm: LLMSettings = field(default_factory=LLMSettings)
    loop: LoopLimits = field(default_factory=LoopLimits)
    tools: ToolLimits = field(default_factory=ToolLimits)
    cli: CliSettings = field(default_factory=CliSettings)
    compact: CompactPolicy = field(default_factory=CompactPolicy)

    def __post_init__(self) -> None:
        self.workspace = Path(self.workspace).expanduser().resolve()
        if not self.workspace.is_dir():
            raise ConfigError(f"工作目录不存在或不是目录：{self.workspace}")
        if not self.llm.api_key:
            raise ConfigError(
                "未找到 API Key。请在 .env 中设置 DEEPSEEK_API_KEY，或通过 --api-key 传入。"
            )

    @property
    def state_dir(self) -> Path:
        """工作区内存放日志等运行时文件的目录。"""
        return self.workspace / self.cli.state_dirname

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"

    @classmethod
    def load(cls, **overrides: Any) -> AgentSettings:
        """按 默认值 < 环境变量 < overrides 装配，见 ``settings.loader``。"""
        from .loader import load_settings

        return load_settings(**overrides)
