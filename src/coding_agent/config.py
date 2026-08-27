"""运行时配置。

优先级：命令行参数 > 环境变量 > 内置默认值。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .errors import ConfigError

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"

# 环境变量名 -> Config 字段名
_ENV_MAPPING = {
    "DEEPSEEK_API_KEY": "api_key",
    "DEEPSEEK_BASE_URL": "base_url",
    "DEEPSEEK_MODEL": "model",
    "CODING_AGENT_MAX_ITERATIONS": "max_iterations",
    "CODING_AGENT_COMMAND_TIMEOUT": "command_timeout",
    "CODING_AGENT_TEMPERATURE": "temperature",
    "CODING_AGENT_LOG_LEVEL": "log_level",
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
    # LLM 请求失败后的最大重试次数（仅对可重试错误生效）
    max_retries: int = 3

    # Agent Loop 终止条件
    max_iterations: int = 30
    max_consecutive_tool_failures: int = 3
    max_repeated_calls: int = 3

    # 工具行为
    command_timeout: int = 60
    max_tool_output_chars: int = 20_000
    max_file_read_chars: int = 60_000
    max_dir_entries: int = 400

    # 安全
    yolo: bool = False

    # 日志：文件始终记录 DEBUG。verbose 才会再打到 stderr（避免和 rich 界面叠在一起）
    log_level: str = "INFO"
    verbose: bool = False

    # 元数据目录（会话、临时文件），位于 workspace 之下
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
        """按 默认值 < 环境变量 < overrides 的顺序装配配置。

        ``overrides`` 中值为 None 的键会被忽略，方便直接把 argparse 的结果传进来。
        """
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
    """把环境变量字符串转换成字段声明的类型。"""
    # dataclass 在 ``from __future__ import annotations`` 下拿到的是字符串形式的类型名
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
    return raw
