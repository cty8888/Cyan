"""权限判定入口：沙箱 → deny → 关键删除询问 → ask → 只读 bash / allow → 三种模式与会话白名单。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..tools.types import ToolCapability
from . import allowlist, command_paths
from .floor import critical_rm_reason
from .messages import (
    NO_PERMISSION_RULE_MSG,
    PLAN_EXEC_MSG,
    PLAN_WRITE_MSG,
)
from .policy import RuleSet
from .settings_file import append_local_allow
from .shell import is_accept_edits_fs_command, is_readonly_command
from .types import ApprovalDecision, ApprovalRequest, DenyReason, PermissionMode, PermissionOutcome

if TYPE_CHECKING:
    from ..session import WorkspaceAccess
    from ..tools.base import Tool


class PermissionManager:
    """按模式与声明式规则判定一次工具调用该放行、拒绝还是等人审批。"""

    def __init__(self, workspace: Path, *, home: Path | None = None) -> None:
        self.workspace = Path(workspace).resolve()
        self.home = Path(home).resolve() if home is not None else None
        self.ruleset = RuleSet.load(self.workspace, home=self.home)

    @property
    def configured_mode(self) -> PermissionMode | None:
        """设置文件里的 defaultMode；未写则为 None。"""
        return self.ruleset.default_mode

    def reload(self) -> None:
        self.ruleset = RuleSet.load(self.workspace, home=self.home)

    def hidden_tool_names(self) -> set[str]:
        return self.ruleset.hidden_tool_names()

    def evaluate(
        self,
        tool: Tool,
        args: dict[str, Any],
        *,
        mode: PermissionMode,
        always_allowed: set[str],
        workspace_access: WorkspaceAccess | None = None,
    ) -> PermissionOutcome:
        """判定本次工具调用的权限结果。"""

        if tool.capability is ToolCapability.EXEC:
            outside = command_paths.outside_workspace_reason(
                self.workspace, str(args.get("command") or ""), self._exec_cwd(workspace_access)
            )
            if outside:
                return PermissionOutcome.deny(DenyReason.POLICY_BLOCKED, outside)

        deny_hit = self.ruleset.match_deny(tool, args, self.workspace)
        if deny_hit is None and tool.capability is ToolCapability.EXEC:
            path_reason = command_paths.denied_path_reason(
                self.workspace, str(args.get("command") or ""), self._exec_cwd(workspace_access)
            )
            if path_reason:
                return PermissionOutcome.deny(DenyReason.RESTRICTED, path_reason)
        if deny_hit is not None:
            kind = (
                DenyReason.RESTRICTED if deny_hit.rule.family in {"write", "read"} else DenyReason.POLICY_BLOCKED
            )
            return PermissionOutcome.deny(kind, deny_hit.message)

        if tool.capability is ToolCapability.EXEC:
            crit = critical_rm_reason(
                str(args.get("command") or ""),
                workspace=self.workspace,
                cwd=self._exec_cwd(workspace_access),
            )
            if crit:
                return self._need_approval(
                    tool, args, force=True, reason=crit, workspace_access=workspace_access
                )

        if tool.name == "todo_write":
            # 任务规划本身不改文件系统，跟模式无关——Plan 模式下也要能列计划。
            return PermissionOutcome.allow()

        if tool.name == "memory_write":
            if mode is PermissionMode.PLAN:
                return PermissionOutcome.deny(DenyReason.MODE_BLOCKED, PLAN_WRITE_MSG)
            return PermissionOutcome.allow()

        if tool.capability is ToolCapability.READ:
            ask_hit = self.ruleset.match_ask(tool, args, self.workspace)
            if ask_hit:
                return self._need_approval(
                    tool, args, force=True, reason=ask_hit.message, workspace_access=workspace_access
                )
            return PermissionOutcome.allow()

        if mode is PermissionMode.PLAN:
            if tool.capability is ToolCapability.WRITE:
                return PermissionOutcome.deny(DenyReason.MODE_BLOCKED, PLAN_WRITE_MSG)
            if tool.capability is ToolCapability.EXEC:
                command = str(args.get("command") or "")
                if not is_readonly_command(command):
                    return PermissionOutcome.deny(DenyReason.MODE_BLOCKED, PLAN_EXEC_MSG)

        ask_hit = self.ruleset.match_ask(tool, args, self.workspace)
        if ask_hit:
            return self._need_approval(
                tool, args, force=True, reason=ask_hit.message, workspace_access=workspace_access
            )

        if tool.capability is ToolCapability.EXEC:
            forced = command_paths.forced_exec_reason(
                self.workspace, str(args.get("command") or ""), self._exec_cwd(workspace_access)
            )
            if forced:
                return self._need_approval(
                    tool, args, force=True, reason=forced, workspace_access=workspace_access
                )
            if is_readonly_command(str(args.get("command") or "")):
                return PermissionOutcome.allow()

        if self.ruleset.match_allow(tool, args, self.workspace):
            return PermissionOutcome.allow()

        if mode is PermissionMode.ACCEPT_EDITS:
            if tool.capability is ToolCapability.WRITE:
                return PermissionOutcome.allow()
            if tool.capability is ToolCapability.EXEC and is_accept_edits_fs_command(
                str(args.get("command") or "")
            ):
                return PermissionOutcome.allow()

        if allowlist.is_always_allowed(self.workspace, tool, args, always_allowed):
            return PermissionOutcome.allow()

        if tool.capability in {ToolCapability.WRITE, ToolCapability.EXEC}:
            return self._need_approval(
                tool, args, force=False, reason=None, workspace_access=workspace_access
            )

        return PermissionOutcome.deny(DenyReason.UNKNOWN, NO_PERMISSION_RULE_MSG)

    def apply_decision(
        self,
        decision: ApprovalDecision,
        tool: Tool,
        args: dict[str, Any],
        always_allowed: set[str],
        *,
        force: bool = False,
    ) -> bool:
        """落实审批结果。``ALLOW_ALWAYS`` 写入会话白名单；bash 再持久化到 local。"""
        if decision is ApprovalDecision.ALLOW_ALWAYS and not force:
            allowlist.remember(self.workspace, tool, args, always_allowed)
            persisted = False
            for persist in allowlist.persistable_allow_rules(self.workspace, tool, args):
                append_local_allow(self.workspace, persist)
                persisted = True
            if persisted:
                self.reload()
        return decision is not ApprovalDecision.DENY

    def _exec_cwd(self, workspace_access: WorkspaceAccess | None) -> Path:
        if workspace_access is not None and workspace_access.bash_cwd is not None:
            return workspace_access.bash_cwd
        return self.workspace

    def _need_approval(
        self,
        tool: Tool,
        args: dict[str, Any],
        *,
        force: bool,
        reason: str | None,
        workspace_access: WorkspaceAccess | None = None,
    ) -> PermissionOutcome:
        """组装审批请求：摘要来自 ``tool.describe()``；``force`` 时不提供「始终允许」。"""
        summary, detail, detail_format = tool.describe(
            args, self.workspace, workspace_access=workspace_access
        )
        request = ApprovalRequest(
            tool_name=tool.name,
            capability=tool.capability.value,
            summary=summary,
            detail=detail,
            detail_format=detail_format,
            force=force,
            reason=reason,
            always_label=None if force else allowlist.always_label(self.workspace, tool, args),
        )
        return PermissionOutcome.need_approval(request)


def initial_permission_mode(
    settings_mode: PermissionMode,
    *,
    override: PermissionMode | None,
    configured: PermissionMode | None,
) -> PermissionMode:
    """CLI ``--mode`` / ``CYAN_MODE`` 优先于设置文件 defaultMode。"""
    if override is not None:
        return override
    if os.getenv("CYAN_MODE"):
        return settings_mode
    if configured is not None:
        return configured
    return settings_mode
