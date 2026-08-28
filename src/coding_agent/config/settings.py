"""应用级运行时配置。

优先级：命令行参数 > 环境变量 > 内置默认值。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from ..errors import ConfigError
from ..security.modes import ExecutionMode
from .tool import ToolConfig

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"

# 环境变量名 -> Config 字段名
_ENV_MAPPING = {
    "DEEPSEEK_API_KEY": "api_key",
    "DEEPSEEK_BASE_URL": "base_url",
    "DEEPSEEK_MODEL": "model",
    "CODING_AGENT_MAX_ITERATIONS": "max_iterations",
    "CODING_AGENT_TEMPERATURE": "temperature",
    "CODING_AGENT_LOG_LEVEL": "log_level",
    "CODING_AGENT_MODE": "execution_mode",
}


@dataclass
class Config:
    """一次运行的全部可调参数。"""

    api_key: str
    workspace: Path

    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    temperature: float = 0.0
    request_timeout: float = 180.0
    max_retries: int = 3

    max_iterations: int = 30
    max_consecutive_tool_failures: int = 3
    max_repeated_calls: int = 3

    tool: ToolConfig = field(default_factory=ToolConfig)

    log_level: str = "INFO"
    verbose: bool = False
    execution_mode: ExecutionMode = ExecutionMode.AGENT

    state_dirname: str = ".coding_agent"

    def __post_init__(self) -> None:
        self.workspace = Path(self.workspace).expanduser().resolve()
        if not self.workspace.is_dir():
            raise ConfigError(f"工作目录不存在或不是目录：{self.workspace}")
        if not self.api_key:
            raise ConfigError(
                "未找到 API Key。请在 .env 中设置 DEEPSEEK_API_KEY，或通过 --api-key 传入。"
            )

    @property
    def state_dir(self) -> Path:
        return self.workspace / self.state_dirname

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"

    @classmethod
    def load(cls, **overrides: Any) -> Config:
        """按 默认值 < 环境变量 < overrides 的顺序装配配置。"""
        load_dotenv()

        values: dict[str, Any] = {"workspace": Path.cwd()}
        field_types = {f.name: f.type for f in fields(cls)}

        for env_name, field_name in _ENV_MAPPING.items():
            raw = os.getenv(env_name)
            if raw is None or raw == "":
                continue
            values[field_name] = _coerce(raw, field_types[field_name], env_name)

        for key, value in overrides.items():
            if value is None:
                continue
            if key not in field_types:
                raise ConfigError(f"未知配置项：{key}")
            values[key] = value

        values.setdefault("api_key", "")
        return cls(**values)


def _coerce(raw: str, target_type: Any, source: str) -> Any:
    type_name = target_type if isinstance(target_type, str) else getattr(target_type, "__name__", "")
    try:
        if type_name == "int":
            return int(raw)
        if type_name == "float":
            return float(raw)
        if type_name == "bool":
            return raw.strip().lower() in {"1", "true", "yes", "on"}
    except ValueError as exc:
        raise ConfigError(f"环境变量 {source} 的值 {raw!r} 无法转换为 {type_name}") from exc
    if type_name == "ExecutionMode":
        try:
            return ExecutionMode.parse(raw)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
    return raw
