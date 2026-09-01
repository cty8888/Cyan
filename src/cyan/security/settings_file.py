"""读写权限设置文件：内置默认 + 用户 + 项目 + local。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..logutil import get_logger
from .rule_syntax import ParsedRule, parse_rule
from .types import PermissionMode

logger = get_logger("security.settings")

RuleKind = Literal["allow", "ask", "deny"]
RuleSource = Literal["builtin", "user", "project", "local"]

DEFAULTS_PATH = Path(__file__).with_name("defaults.json")
PROJECT_SETTINGS_NAME = "settings.json"
LOCAL_SETTINGS_NAME = "settings.local.json"


@dataclass(frozen=True)
class PermissionRule:
    """一条已加载的规则，带来源，便于 /permissions 展示和删除。"""

    kind: RuleKind
    parsed: ParsedRule
    source: RuleSource

    @property
    def family(self) -> str:
        return self.parsed.family

    @property
    def pattern(self) -> str | None:
        return self.parsed.pattern

    @property
    def raw(self) -> str:
        return self.parsed.raw

    @property
    def removable(self) -> bool:
        return self.source != "builtin"

    @property
    def inert(self) -> bool:
        return self.parsed.inert

    @property
    def param(self) -> str | None:
        return self.parsed.param

    @property
    def tool_name(self) -> str | None:
        return self.parsed.tool_name


@dataclass
class LoadedSettings:
    """合并后的规则列表与可选的 defaultMode。"""

    rules: list[PermissionRule] = field(default_factory=list)
    default_mode: PermissionMode | None = None
    warnings: list[str] = field(default_factory=list)


def project_settings_path(workspace: Path) -> Path:
    return Path(workspace) / ".cyan" / PROJECT_SETTINGS_NAME


def local_settings_path(workspace: Path) -> Path:
    return Path(workspace) / ".cyan" / LOCAL_SETTINGS_NAME


def user_settings_path(home: Path) -> Path:
    return Path(home) / "settings.json"


def load_builtin_settings() -> LoadedSettings:
    """只加载随包 defaults.json。"""
    loaded = LoadedSettings()
    data, warning = _read_json(DEFAULTS_PATH, required=True)
    if warning:
        loaded.warnings.append(warning)
        logger.warning("%s", warning)
    if data is not None:
        _ingest(data, "builtin", loaded)
    return loaded


def load_merged_settings(workspace: Path, *, home: Path | None = None) -> LoadedSettings:
    """内置 + 用户（home 非空）+ 项目 + local。数组合并，非法规则记警告并跳过。

    ``home is None`` 时不读用户文件，供测试隔离真实 ``~/.cyan``。
    """
    loaded = LoadedSettings()
    layers: list[tuple[RuleSource, Path, bool]] = [
        ("builtin", DEFAULTS_PATH, True),
    ]
    if home is not None:
        layers.append(("user", user_settings_path(home), False))
    layers.append(("project", project_settings_path(workspace), False))
    layers.append(("local", local_settings_path(workspace), False))

    modes: dict[RuleSource, PermissionMode] = {}
    for source, path, required in layers:
        data, warning = _read_json(path, required=required)
        if warning:
            loaded.warnings.append(warning)
            logger.warning("%s", warning)
        if data is None:
            continue
        _ingest(data, source, loaded)
        mode = _parse_default_mode(data, source)
        if mode is not None:
            modes[source] = mode

    loaded.default_mode = _pick_mode(modes)
    return loaded


def append_local_allow(workspace: Path, raw: str) -> None:
    """把一条 allow 规则追加到 ``settings.local.json``（已存在则跳过）。"""
    path = local_settings_path(workspace)
    data = _read_json(path, required=False)[0] or {}
    permissions = data.setdefault("permissions", {})
    allow: list[str] = list(permissions.get("allow") or [])
    if raw in allow:
        return
    allow.append(raw)
    permissions["allow"] = allow
    _write_json(path, data)


def add_local_rule(workspace: Path, kind: RuleKind, raw: str) -> None:
    """往 local 文件追加一条规则。"""
    parse_rule(raw)
    path = local_settings_path(workspace)
    data = _read_json(path, required=False)[0] or {}
    permissions = data.setdefault("permissions", {})
    items: list[str] = list(permissions.get(kind) or [])
    if raw not in items:
        items.append(raw)
        permissions[kind] = items
        _write_json(path, data)


def remove_local_rule(workspace: Path, raw: str) -> bool:
    """从 local 文件的 allow/ask/deny 里删掉 ``raw``。返回是否删到了。"""
    return _remove_from_path(local_settings_path(workspace), raw)


def remove_rule(
    workspace: Path, raw: str, *, home: Path | None = None
) -> tuple[Literal["removed", "builtin", "missing"], RuleSource | None]:
    """从 local → 项目 → 用户依次删第一条匹配。不能删内置。"""
    if _remove_from_path(local_settings_path(workspace), raw):
        return "removed", "local"
    if _remove_from_path(project_settings_path(workspace), raw):
        return "removed", "project"
    if home is not None and _remove_from_path(user_settings_path(home), raw):
        return "removed", "user"
    if any(rule.raw == raw for rule in load_builtin_settings().rules):
        return "builtin", "builtin"
    return "missing", None


def _remove_from_path(path: Path, raw: str) -> bool:
    data = _read_json(path, required=False)[0]
    if data is None:
        return False
    permissions = data.get("permissions")
    if not isinstance(permissions, dict):
        return False
    found = False
    for kind in ("allow", "ask", "deny"):
        items = permissions.get(kind)
        if not isinstance(items, list):
            continue
        filtered = [item for item in items if str(item) != raw]
        if len(filtered) != len(items):
            permissions[kind] = filtered
            found = True
    if found:
        _write_json(path, data)
    return found


def _ingest(data: dict[str, Any], source: RuleSource, loaded: LoadedSettings) -> None:
    permissions = data.get("permissions")
    if not isinstance(permissions, dict):
        return
    for kind in ("deny", "ask", "allow"):
        items = permissions.get(kind)
        if items is None:
            continue
        if not isinstance(items, list):
            warning = f"{source} 的 permissions.{kind} 不是数组，已忽略"
            loaded.warnings.append(warning)
            logger.warning("%s", warning)
            continue
        for item in items:
            text = str(item).strip()
            if text.lower().startswith("mcp__") and "(" in text:
                warning = f"{source} 跳过带括号的 MCP 规则 {text}（请用 --disallowedTools）"
                loaded.warnings.append(warning)
                logger.warning("%s", warning)
                continue
            try:
                parsed = parse_rule(text)
            except ValueError as exc:
                warning = f"{source} 跳过非法规则 {text!r}：{exc}"
                loaded.warnings.append(warning)
                logger.warning("%s", warning)
                continue
            if parsed.inert:
                warning = _inert_warning(source, parsed)
                loaded.warnings.append(warning)
                logger.warning("%s", warning)
            elif parsed.param is not None and parsed.param != "domain" and kind == "allow":
                warning = (
                    f"{source} 的 {text} 不会放行整次调用；"
                    "参数规则只用于 deny/ask，allow 请用工具自己的指定符"
                    "（Bash(命令) / Read(路径) / WebFetch(domain:host)）"
                )
                loaded.warnings.append(warning)
                logger.warning("%s", warning)
            loaded.rules.append(PermissionRule(kind=kind, parsed=parsed, source=source))  # type: ignore[arg-type]


def _parse_default_mode(data: dict[str, Any], source: RuleSource) -> PermissionMode | None:
    permissions = data.get("permissions")
    if not isinstance(permissions, dict):
        return None
    raw = permissions.get("defaultMode")
    if raw is None or raw == "":
        return None
    try:
        return PermissionMode(str(raw))
    except ValueError:
        logger.warning("%s 的 defaultMode=%r 无效，已忽略", source, raw)
        return None


def _inert_warning(source: RuleSource, parsed: ParsedRule) -> str:
    if parsed.param == "command":
        return f"{source} 的 {parsed.raw} 会被复合命令绕过，请改用 Bash(...)"
    if parsed.param in {"path", "file_path"}:
        return f"{source} 的 {parsed.raw} 不能匹配路径，请改用 Read(...) / Edit(...)"
    if parsed.param == "url":
        return f"{source} 的 {parsed.raw} 不能匹配 url，请改用 WebFetch(domain:...)"
    if parsed.param is not None:
        return f"{source} 的 {parsed.raw} 不能用主要内容参数匹配，已忽略"
    return f"{source} 的 {parsed.raw} 不会用于路径权限检查，请改用 Edit(...)"


def _pick_mode(modes: dict[RuleSource, PermissionMode]) -> PermissionMode | None:
    """local > project > user；内置 defaults.json 不设 defaultMode。"""
    for source in ("local", "project", "user"):
        if source in modes:
            return modes[source]
    return None


def _read_json(path: Path, *, required: bool) -> tuple[dict[str, Any] | None, str | None]:
    if not path.is_file():
        if required:
            return None, f"找不到内置规则文件：{path}"
        return None, None
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"无法读取 {path}：{exc}"
    if not isinstance(data, dict):
        return None, f"{path} 根节点必须是 JSON 对象"
    return data, None


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
