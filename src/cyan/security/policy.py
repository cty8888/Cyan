"""规则集合：按 deny → ask → allow 匹配一次工具调用。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ..tools.types import ToolCapability
from .paths import resolve_path, write_target_display
from .rule_syntax import (
    FAMILIES,
    bash_subjects,
    domain_from_args,
    match_bash_pattern,
    match_param_value,
    match_path_pattern,
)
from .settings_file import LoadedSettings, PermissionRule, RuleKind, load_merged_settings
from .types import PermissionMode

if TYPE_CHECKING:
    from ..tools.base import Tool

BARE_DENY_TOOLS: dict[str, frozenset[str]] = {
    "bash": frozenset({"bash"}),
    "write": frozenset({"write_file", "edit_file", "memory_write"}),
    "read": frozenset({"read_file", "list_dir", "glob", "grep", "memory_list", "memory_read"}),
}


@dataclass(frozen=True)
class RuleHit:
    rule: PermissionRule
    message: str


@dataclass
class RuleSet:
    """一次加载的全部规则，以及文件里的 defaultMode。"""

    rules: list[PermissionRule]
    default_mode: PermissionMode | None = None
    warnings: list[str] | None = None
    home: Path | None = None

    @classmethod
    def load(cls, workspace: Path, *, home: Path | None = None) -> RuleSet:
        loaded = load_merged_settings(workspace, home=home)
        return cls.from_loaded(loaded, home=home)

    @classmethod
    def builtin(cls) -> RuleSet:
        """只读 defaults.json，执行层二次拦截用。"""
        from .settings_file import load_builtin_settings

        return cls.from_loaded(load_builtin_settings())

    @classmethod
    def from_loaded(cls, loaded: LoadedSettings, *, home: Path | None = None) -> RuleSet:
        return cls(rules=loaded.rules, default_mode=loaded.default_mode, warnings=loaded.warnings, home=home)

    def of_kind(self, kind: RuleKind) -> list[PermissionRule]:
        return [rule for rule in self.rules if rule.kind == kind]

    def hidden_tool_names(self) -> set[str]:
        """裸名 deny（无指定符）从模型上下文摘掉的工具。"""
        names: set[str] = set()
        for rule in self.of_kind("deny"):
            if rule.pattern is not None or rule.param is not None:
                continue
            if rule.family in BARE_DENY_TOOLS:
                names.update(BARE_DENY_TOOLS[rule.family])
            elif rule.tool_name:
                names.add(rule.tool_name)
        return names

    def match_deny(self, tool: Tool, args: dict[str, Any], workspace: Path) -> RuleHit | None:
        hit = self._first_match("deny", tool, args, workspace, deep=True)
        if hit is not None:
            return hit
        if _family_for_tool(tool) != "write":
            return None
        path = _path_context(args, workspace)
        if path is None:
            return None
        relative, absolute = path
        for rule in self.of_kind("deny"):
            if rule.inert or rule.param is not None:
                continue
            if rule.family == "read" and self._matches_path(
                rule, relative, deep=True, workspace=workspace, absolute=absolute
            ):
                return RuleHit(rule, _path_message(rule, relative))
        return None

    def match_ask(self, tool: Tool, args: dict[str, Any], workspace: Path) -> RuleHit | None:
        return self._first_match("ask", tool, args, workspace, deep=True)

    def match_allow(self, tool: Tool, args: dict[str, Any], workspace: Path) -> bool:
        if self._param_rule_allows(tool, args):
            return True
        family = _family_for_tool(tool)
        if family is None:
            return False
        allow = [
            rule
            for rule in self.of_kind("allow")
            if rule.family == family and not rule.inert and rule.param is None
        ]
        if not allow:
            return False
        if family == "bash":
            subjects = bash_subjects(str(args.get("command") or ""), peel_all_assignments=False)
            if not subjects:
                return False
            return all(_bash_covered(subject, allow) for subject in subjects)
        path = _path_context(args, workspace)
        if path is None:
            return False
        relative, absolute = path
        return any(
            self._matches_path(rule, relative, deep=False, workspace=workspace, absolute=absolute)
            for rule in allow
        )

    def path_is_sensitive(self, relative: str) -> str | None:
        """grep/glob：只看 read 的 ask/deny，写保护名单不会把 .vscode 从搜索结果里藏掉。"""
        for kind in ("deny", "ask"):
            for rule in self.of_kind(kind):  # type: ignore[arg-type]
                if rule.inert or rule.param is not None:
                    continue
                if rule.family == "read" and self._matches_path(rule, relative, deep=True):
                    return _path_message(rule, relative)
        return None

    def path_write_denied(self, relative: str) -> str | None:
        for rule in self.of_kind("deny"):
            if rule.inert or rule.param is not None:
                continue
            if rule.family in {"write", "read"} and self._matches_path(rule, relative, deep=True):
                return _path_message(rule, relative)
        return None

    def path_read_denied(self, relative: str) -> str | None:
        for rule in self.of_kind("deny"):
            if rule.inert or rule.param is not None:
                continue
            if rule.family == "read" and self._matches_path(rule, relative, deep=True):
                return _path_message(rule, relative)
        return None

    def path_write_needs_confirm(self, relative: str) -> str | None:
        """bash 写触点：write ask，或 read ask（写密钥也要确认）。"""
        for rule in self.of_kind("ask"):
            if rule.inert or rule.param is not None:
                continue
            if rule.family in {"read", "write"} and self._matches_path(rule, relative, deep=True):
                return _path_message(rule, relative)
        return None

    def bash_denied(self, command: str) -> str | None:
        for rule in self.of_kind("deny"):
            if rule.family != "bash" or rule.inert or rule.param is not None:
                continue
            hit = _bash_hit(rule, command)
            if hit:
                return hit
        return None

    def _matches_path(
        self,
        rule: PermissionRule,
        relative: str,
        *,
        deep: bool,
        workspace: Path | None = None,
        absolute: Path | None = None,
    ) -> bool:
        if rule.pattern is None:
            return True
        return match_path_pattern(
            rule.pattern,
            relative,
            deep=deep,
            absolute=absolute,
            workspace=workspace,
            config_home=self.home,
            source=rule.source,
        )

    def _param_rule_allows(self, tool: Tool, args: dict[str, Any]) -> bool:
        """allow 里只有 ``domain:`` 这类工具自身指定符能放行整次调用。"""
        for rule in self.of_kind("allow"):
            if rule.inert or rule.param != "domain":
                continue
            if _named_tool_matches(rule, tool) and _param_matches(rule, args):
                return True
        return False

    def _first_match(
        self,
        kind: RuleKind,
        tool: Tool,
        args: dict[str, Any],
        workspace: Path,
        *,
        deep: bool,
    ) -> RuleHit | None:
        for rule in self.of_kind(kind):
            if rule.inert:
                continue
            if rule.param is not None:
                if kind == "allow" and rule.param != "domain":
                    continue
                if _named_tool_matches(rule, tool) and _param_matches(rule, args):
                    return RuleHit(rule, _param_message(rule))
                continue
            if rule.family not in FAMILIES:
                if not _named_tool_matches(rule, tool):
                    continue
                if rule.pattern in {None, "*"}:
                    return RuleHit(rule, _param_message(rule))
                continue
            family = _family_for_tool(tool)
            if family is None or rule.family != family:
                continue
            if family == "bash":
                message = _bash_hit(rule, str(args.get("command") or ""))
                if message:
                    return RuleHit(rule, message)
                continue
            path = _path_context(args, workspace)
            if path is None:
                continue
            relative, absolute = path
            if self._matches_path(
                rule, relative, deep=deep, workspace=workspace, absolute=absolute
            ):
                return RuleHit(rule, _path_message(rule, relative))
        return None


def _named_tool_matches(rule: PermissionRule, tool: Tool) -> bool:
    expected = (rule.tool_name or rule.family).lower()
    return tool.name.lower() == expected


def _param_matches(rule: PermissionRule, args: dict[str, Any]) -> bool:
    if rule.param is None or rule.pattern is None:
        return False
    if rule.param == "domain":
        host = domain_from_args(args)
        if host is None:
            return False
        return match_bash_pattern(rule.pattern, host)
    if rule.param not in args:
        return False
    return match_param_value(rule.pattern, args.get(rule.param))


def _param_message(rule: PermissionRule) -> str:
    if rule.kind == "deny":
        return f"规则 {rule.raw} 拒绝了此次调用。"
    return f"规则 {rule.raw} 要求确认。"


def _family_for_tool(tool: Tool) -> Literal["bash", "read", "write"] | None:
    if tool.name == "memory_write":
        return None
    if tool.name == "bash" or tool.capability is ToolCapability.EXEC:
        return "bash"
    if tool.capability is ToolCapability.WRITE:
        return "write"
    if tool.capability is ToolCapability.READ:
        return "read"
    return None


def _path_context(args: dict[str, Any], workspace: Path) -> tuple[str, Path] | None:
    relative = write_target_display(workspace, args)
    if relative is None:
        return None
    raw = args.get("path")
    try:
        resolved = resolve_path(workspace, str(raw))
    except Exception:
        try:
            candidate = Path(str(raw)).expanduser()
            resolved = candidate.resolve() if candidate.is_absolute() else (workspace / relative).resolve()
        except (OSError, RuntimeError, ValueError):
            resolved = workspace / relative
    return relative, resolved


def _bash_hit(rule: PermissionRule, command: str) -> str | None:
    subjects = bash_subjects(command)
    if not subjects:
        if rule.pattern is None:
            return f"规则 {rule.raw} 拒绝了此次执行。"
        return None
    for subject in subjects:
        if rule.pattern is None or match_bash_pattern(rule.pattern, subject):
            return f"规则 {rule.raw} 拒绝了命令。" if rule.kind == "deny" else f"规则 {rule.raw} 要求确认。"
    return None


def _bash_covered(subject: str, allow: list[PermissionRule]) -> bool:
    for rule in allow:
        if rule.pattern is None or match_bash_pattern(rule.pattern, subject):
            return True
    return False


def _path_message(rule: PermissionRule, relative: str) -> str:
    if rule.family == "write" and ".git" in (rule.pattern or ""):
        if rule.kind == "deny":
            return f"{relative} 位于仓库元数据目录，Agent 不能直接写入，请手动处理。"
        return f"{relative} 位于仓库元数据目录，需要确认。"
    if rule.kind == "deny":
        return f"规则 {rule.raw} 拒绝了 {relative}。"
    return f"{relative} 命中规则 {rule.raw}，需要确认。"
