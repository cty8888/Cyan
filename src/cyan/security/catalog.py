"""从 defaults.json 读取内置 shell 名单。

只读命令、包装前缀、安全环境变量、acceptEdits 文件系统命令写在 JSON 里，
改名单不必动判定代码。剥包装的参数解析、git 条件只读仍在 Python。
用户 / 项目 settings 不能覆盖这份名单。

``pytest`` 列在 ``readonlyBinaries``：README / PLAN_EXEC_MSG 拿它当 Plan 模式的典型例子。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

DEFAULTS_PATH = Path(__file__).with_name("defaults.json")
UNWRAP_KINDS = frozenset(
    {"env", "timeout", "nice", "stdbuf", "command", "exec", "passthrough"}
)


@dataclass(frozen=True)
class ShellCatalog:
    """defaults.json ``shell`` 段解析后的名单。"""

    readonly_binaries: frozenset[str]
    git_readonly_subcommands: frozenset[str]
    accept_edits_fs: frozenset[str]
    safe_env_names: frozenset[str]
    unwrap: dict[str, str]


@lru_cache(maxsize=1)
def shell_catalog() -> ShellCatalog:
    """加载并缓存内置 shell 名单。文件坏了就直接报错。"""
    data = json.loads(DEFAULTS_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("defaults.json 必须是对象")
    raw = data.get("shell")
    if not isinstance(raw, dict):
        raise ValueError("defaults.json 缺少 shell 对象")
    return ShellCatalog(
        readonly_binaries=_string_set(raw, "readonlyBinaries"),
        git_readonly_subcommands=_string_set(raw, "gitReadonlySubcommands"),
        accept_edits_fs=_string_set(raw, "acceptEditsFs"),
        safe_env_names=_string_set(raw, "safeEnvNames"),
        unwrap=_unwrap_map(raw.get("unwrap")),
    )


def _string_set(data: dict[str, Any], key: str) -> frozenset[str]:
    items = data.get(key)
    if not isinstance(items, list) or not items:
        raise ValueError(f"defaults.json shell.{key} 必须是非空字符串数组")
    names = [str(item).strip() for item in items]
    if any(not name for name in names):
        raise ValueError(f"defaults.json shell.{key} 含空字符串")
    return frozenset(names)


def _unwrap_map(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ValueError("defaults.json shell.unwrap 必须是对象")
    unknown = set(raw) - UNWRAP_KINDS
    missing = UNWRAP_KINDS - set(raw)
    if unknown:
        raise ValueError(f"defaults.json shell.unwrap 未知类型：{sorted(unknown)}")
    if missing:
        raise ValueError(f"defaults.json shell.unwrap 缺少类型：{sorted(missing)}")
    mapping: dict[str, str] = {}
    for kind, names in raw.items():
        if not isinstance(names, list) or not names:
            raise ValueError(f"defaults.json shell.unwrap.{kind} 必须是非空字符串数组")
        for item in names:
            name = str(item).strip()
            if not name:
                raise ValueError(f"defaults.json shell.unwrap.{kind} 含空字符串")
            if name in mapping:
                raise ValueError(f"defaults.json 包装名 {name} 重复")
            mapping[name] = str(kind)
    return mapping
