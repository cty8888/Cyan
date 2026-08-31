"""从 defaults.json 读取内置 shell 名单。

命令名、只读相关 flag、包装前缀、写目标 flag 表写在 JSON 里，改名单不必动判定代码。
切段符、包装参数解析、git 全局选项、路径形状检测仍在 Python。
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
    python_binaries: frozenset[str]
    find_dangerous_flags: frozenset[str]
    sed_inplace_flags: frozenset[str]
    sort_output_flags: frozenset[str]
    env_dump_commands: frozenset[str]
    chdir_commands: frozenset[str]
    unresolved_chdir_commands: frozenset[str]
    write_all: frozenset[str]
    write_last: frozenset[str]
    read_args: frozenset[str]
    opaque_heads: frozenset[str]
    stdin_shells: frozenset[str]
    interpreters: frozenset[str]
    inplace_interpreters: frozenset[str]
    git_content_subcommands: frozenset[str]
    recursive_search_heads: frozenset[str]
    grep_heads: frozenset[str]
    recursive_grep_flags: frozenset[str]
    write_flag_next: dict[str, frozenset[str]]
    write_flag_eq: dict[str, tuple[str, ...]]
    upload_flag_next: dict[str, frozenset[str]]
    upload_flag_eq: dict[str, tuple[str, ...]]
    upload_always_path_flags: frozenset[str]


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
        python_binaries=_string_set(raw, "pythonBinaries"),
        find_dangerous_flags=_string_set(raw, "findDangerousFlags"),
        sed_inplace_flags=_string_set(raw, "sedInplaceFlags"),
        sort_output_flags=_string_set(raw, "sortOutputFlags"),
        env_dump_commands=_string_set(raw, "envDumpCommands"),
        chdir_commands=_string_set(raw, "chdirCommands"),
        unresolved_chdir_commands=_string_set(raw, "unresolvedChdirCommands"),
        write_all=_string_set(raw, "writeAll"),
        write_last=_string_set(raw, "writeLast"),
        read_args=_string_set(raw, "readArgs"),
        opaque_heads=_string_set(raw, "opaqueHeads"),
        stdin_shells=_string_set(raw, "stdinShells"),
        interpreters=_string_set(raw, "interpreters"),
        inplace_interpreters=_string_set(raw, "inplaceInterpreters"),
        git_content_subcommands=_string_set(raw, "gitContentSubcommands"),
        recursive_search_heads=_string_set(raw, "recursiveSearchHeads"),
        grep_heads=_string_set(raw, "grepHeads"),
        recursive_grep_flags=_string_set(raw, "recursiveGrepFlags"),
        write_flag_next=_string_set_map(raw.get("writeFlagNext"), "writeFlagNext"),
        write_flag_eq=_string_tuple_map(raw.get("writeFlagEq"), "writeFlagEq"),
        upload_flag_next=_string_set_map(raw.get("uploadFlagNext"), "uploadFlagNext"),
        upload_flag_eq=_string_tuple_map(raw.get("uploadFlagEq"), "uploadFlagEq"),
        upload_always_path_flags=_string_set(raw, "uploadAlwaysPathFlags"),
    )


def _string_set(data: dict[str, Any], key: str) -> frozenset[str]:
    return _string_set_from_list(data.get(key), key)


def _string_set_from_list(items: Any, label: str) -> frozenset[str]:
    if not isinstance(items, list) or not items:
        raise ValueError(f"defaults.json shell.{label} 必须是非空字符串数组")
    names = [str(item).strip() for item in items]
    if any(not name for name in names):
        raise ValueError(f"defaults.json shell.{label} 含空字符串")
    return frozenset(names)


def _string_set_map(raw: Any, label: str) -> dict[str, frozenset[str]]:
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"defaults.json shell.{label} 必须是非空对象")
    return {
        _map_key(key, label): _string_set_from_list(value, f"{label}.{key}")
        for key, value in raw.items()
    }


def _string_tuple_map(raw: Any, label: str) -> dict[str, tuple[str, ...]]:
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"defaults.json shell.{label} 必须是非空对象")
    mapping: dict[str, tuple[str, ...]] = {}
    for key, value in raw.items():
        _string_set_from_list(value, f"{label}.{key}")
        mapping[_map_key(key, label)] = tuple(str(item).strip() for item in value)
    return mapping


def _map_key(key: Any, label: str) -> str:
    name = str(key).strip()
    if not name:
        raise ValueError(f"defaults.json shell.{label} 含空键")
    return name


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
