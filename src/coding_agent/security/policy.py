"""安全策略：路径沙箱与安全规则统一入口。

只回答「这次操作是否触碰安全边界」；要不要问用户、用户同不同意，
由 ``PermissionManager`` 处理。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import BlockedCommandError, PathOutsideWorkspaceError
from . import security_rules as rules

if TYPE_CHECKING:
    from ..tools.base import Tool

_WRITE_REDIRECT = re.compile(r"(?:>>?|\btee\b)\s*(\S+)")


class SecurityPolicy:
    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()

    # ---------------------------------------------------------------- 路径
    def resolve_path(self, raw: str, *, must_exist: bool = False) -> Path:
        """把工具参数中的路径解析为绝对路径，并校验未逃出沙箱。"""
        if raw is None or str(raw).strip() == "":
            raise PathOutsideWorkspaceError("路径不能为空")

        candidate = Path(str(raw)).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate

        resolved = candidate.resolve()
        if resolved != self.workspace and self.workspace not in resolved.parents:
            raise PathOutsideWorkspaceError(
                f"路径 {raw} 解析为 {resolved}，位于工作目录 {self.workspace} 之外，已拒绝访问。"
                "只能操作工作目录内的文件。"
            )
        if must_exist and not resolved.exists():
            raise FileNotFoundError(f"路径不存在：{self.display(resolved)}")
        return resolved

    def display(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.workspace)) or "."
        except ValueError:
            return str(path)

    def is_sensitive(self, path: Path) -> bool:
        return rules.is_sensitive_path(path)

    def is_restricted(self, path: Path) -> bool:
        return rules.is_restricted_path(path)

    # ---------------------------------------------------------------- 命令
    def check_command(self, command: str) -> None:
        """命中黑名单则抛出 ``BlockedCommandError``（执行层兜底）。"""
        reason = rules.match_command_rules(command, rules.BLOCKED_COMMANDS)
        if reason:
            normalized = rules.normalize_command(command)
            raise BlockedCommandError(
                f"命令被安全策略拦截（{reason}）：{normalized}。"
                "该限制无法通过授权绕过，请改用更安全的做法。"
            )

    # -------------------------------------------------------- 统一 concern 入口
    def blocked_concern(self, tool: Tool, args: dict[str, Any]) -> str | None:
        """灾难性操作，write/exec 均检查。"""
        from ..tools.base import RiskLevel

        if tool.risk is RiskLevel.READ:
            return None

        raw_path = args.get("path") or args.get("file")
        if raw_path:
            try:
                self.resolve_path(str(raw_path))
            except PathOutsideWorkspaceError as exc:
                return str(exc)

        command = args.get("command")
        if command:
            return rules.match_command_rules(str(command), rules.BLOCKED_COMMANDS)
        return None

    def restricted_concern(self, tool: Tool, args: dict[str, Any]) -> str | None:
        """强硬限制，write/exec 均检查，Agent/YOLO 直接拒绝。"""
        from ..tools.base import RiskLevel

        if tool.risk is RiskLevel.READ:
            return None

        raw_path = args.get("path") or args.get("file")
        if raw_path:
            try:
                resolved = self.resolve_path(str(raw_path))
            except PathOutsideWorkspaceError:
                return None
            if self.is_restricted(resolved):
                return f"{self.display(resolved)} 属于受保护路径，不允许修改"

        command = args.get("command")
        if command:
            cmd = str(command)
            reason = rules.match_command_rules(cmd, rules.RESTRICTED_COMMANDS)
            if reason:
                return reason
            hit = self._restricted_write_token(cmd)
            if hit:
                return f"命令试图写入受保护路径 {hit}，不允许执行"
        return None

    def sensitive_concern(self, tool: Tool, args: dict[str, Any]) -> str | None:
        """需人工确认的操作，write/exec 均检查。"""
        from ..tools.base import RiskLevel

        if tool.risk is RiskLevel.READ:
            return None

        raw_path = args.get("path") or args.get("file")
        if raw_path:
            try:
                resolved = self.resolve_path(str(raw_path))
            except PathOutsideWorkspaceError:
                return "路径可疑"
            if self.is_sensitive(resolved):
                return f"{self.display(resolved)} 属于敏感文件，每次操作都需要确认"

        command = args.get("command")
        if command:
            cmd = str(command)
            reason = rules.match_command_rules(cmd, rules.SENSITIVE_COMMANDS)
            if reason:
                return reason
            hit = self._path_token_match(cmd, rules.is_sensitive_path)
            if hit:
                return f"命令中出现敏感文件 {hit}，每次执行都需要确认"
        return None

    def _restricted_write_token(self, command: str) -> str | None:
        """仅当命令含重定向写入时才检查受保护路径。"""
        for match in _WRITE_REDIRECT.finditer(command):
            token = match.group(1).strip("'\"")
            if rules.is_restricted_path(Path(token)):
                return token
        return None

    def _path_token_match(self, command: str, predicate) -> str | None:
        for token in rules.iter_command_tokens(command):
            if predicate(Path(token)):
                return token
        return None
