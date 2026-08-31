"""把默认值、环境变量、命令行覆盖装配为 ``AgentSettings``。"""

from __future__ import annotations

import os
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from ..errors import ConfigError
from ..security.types import PermissionMode
from .agent import AgentSettings
from .cli import CliSettings
from .compact import CompactPolicy
from .llm import LLMSettings
from .loop import LoopLimits
from .tools import ToolLimits

# 环境变量名 -> 扁平字段名
_ENV_MAPPING = {
    "DEEPSEEK_API_KEY": "api_key",
    "DEEPSEEK_BASE_URL": "base_url",
    "DEEPSEEK_MODEL": "model",
    "CYAN_MAX_ITERATIONS": "max_iterations",
    "CYAN_MAX_TOKENS": "max_tokens",
    "CYAN_TEMPERATURE": "temperature",
    "CYAN_LOG_LEVEL": "log_level",
    "CYAN_STREAM": "stream",
    "CYAN_MODE": "permission_mode",
    "CYAN_MAX_CONTEXT_TOKENS": "max_context_tokens",
    "CYAN_COMPACT_RESERVE_TOKENS": "reserve_tokens",
    "CYAN_COMPACT_TRIGGER_RATIO": "trigger_ratio",
    "CYAN_COMPACT_KEEP_TURNS": "keep_recent_turns",
}

_NESTED_KEYS = frozenset({"llm", "loop", "tools", "cli", "compact"})


def _load_env_files(workspace: Any) -> None:
    """先读进程 CWD 的 ``.env``，再用工作区 ``.env`` 覆盖，让 ``-w`` 跟对项目密钥。"""
    load_dotenv()
    if workspace is None or workspace == "":
        return
    env_file = Path(workspace).expanduser() / ".env"
    if env_file.is_file():
        load_dotenv(env_file, override=True)


def _field_names(cls: type) -> frozenset[str]:
    """从 dataclass 反射字段名，避免与各域设置手工同步一份名单。"""
    return frozenset(f.name for f in dataclass_fields(cls))


_LLM_FIELDS = _field_names(LLMSettings)
_LOOP_FIELDS = _field_names(LoopLimits)
_CLI_FIELDS = _field_names(CliSettings)
_TOOL_FIELDS = _field_names(ToolLimits)
_COMPACT_FIELDS = _field_names(CompactPolicy)
_KNOWN_FIELDS = (
    {"workspace"}
    | _LLM_FIELDS
    | _LOOP_FIELDS
    | _CLI_FIELDS
    | _TOOL_FIELDS
    | _COMPACT_FIELDS
    | _NESTED_KEYS
)


def load_settings(**overrides: Any) -> AgentSettings:
    """按 默认值 < 环境变量 < 调用方覆盖 的顺序装配。"""
    _load_env_files(overrides.get("workspace"))

    flat: dict[str, Any] = {"workspace": Path.cwd()}
    nested: dict[str, Any] = {}

    for env_name, field_name in _ENV_MAPPING.items():
        raw = os.getenv(env_name)
        if raw is None or raw == "":
            continue
        flat[field_name] = _coerce_flat(field_name, raw, env_name)

    for key, value in overrides.items():
        if value is None:
            continue
        if key in _NESTED_KEYS:
            nested[key] = value
            continue
        if key not in _KNOWN_FIELDS:
            raise ConfigError(f"未知配置项：{key}")
        flat[key] = value

    flat.setdefault("api_key", "")
    return _assemble(flat, nested)


def _assemble(flat: dict[str, Any], nested: dict[str, Any]) -> AgentSettings:
    """把扁平字段拆进 llm/loop/tools/cli；调用方传入的嵌套对象优先。"""
    llm = nested.get("llm") or LLMSettings(**{k: flat[k] for k in _LLM_FIELDS if k in flat})
    loop = nested.get("loop") or LoopLimits(**{k: flat[k] for k in _LOOP_FIELDS if k in flat})
    tools = nested.get("tools") or ToolLimits(**{k: flat[k] for k in _TOOL_FIELDS if k in flat})
    cli = nested.get("cli") or CliSettings(**{k: flat[k] for k in _CLI_FIELDS if k in flat})
    compact = nested.get("compact") or CompactPolicy(**{k: flat[k] for k in _COMPACT_FIELDS if k in flat})
    workspace = flat.get("workspace", Path.cwd())
    return AgentSettings(
        workspace=workspace, llm=llm, loop=loop, tools=tools, cli=cli, compact=compact
    )


def _coerce_flat(field_name: str, raw: str, source: str) -> Any:
    """把环境变量字符串转成对应字段类型。"""
    if field_name in {
        "max_iterations",
        "max_retries",
        "max_tokens",
        "max_consecutive_tool_failures",
        "max_repeated_calls",
        "max_context_tokens",
        "reserve_tokens",
        "keep_recent_turns",
    }:
        return _coerce_int(raw, source)
    if field_name in {"temperature", "request_timeout", "trigger_ratio"}:
        return _coerce_float(raw, source)
    if field_name in {"verbose", "stream"}:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if field_name == "permission_mode":
        try:
            return PermissionMode(raw)
        except ValueError as exc:
            raise ConfigError(
                f"环境变量 {source} 的值 {raw!r} 不是有效的 PermissionMode"
            ) from exc
    return raw


def _coerce_int(raw: str, source: str) -> int:
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"环境变量 {source} 的值 {raw!r} 无法转换为 int") from exc


def _coerce_float(raw: str, source: str) -> float:
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"环境变量 {source} 的值 {raw!r} 无法转换为 float") from exc
