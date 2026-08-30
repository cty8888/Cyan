"""权限判定入口：结合规则表、PermissionMode 与白名单，产出 ALLOW / DENY / NEED_APPROVAL。

判定顺序（Blocked > Restricted > Plan > Sensitive > Normal）：

1. **Blocked**：永远 DENY，任何模式（包括 Bypass）都不能绕过。
2. **Restricted / 路径沙箱**：DENY，不出审批 UI。bash 里能看清的区外路径、写 ``.git/`` 走这里。
3. **READ / Plan**：普通只读放行；读敏感路径仍要确认。Plan 禁止写文件，bash 仅放行只读命令（读 ``.env`` / ``printenv`` / 递归搜索仍要确认）。
4. **Sensitive / CRITICAL**：NEED_APPROVAL 且 force=True，不受「始终允许」或
   AcceptEdits / Bypass 影响。
5. **Normal**：由 PermissionMode 与本会话白名单处理。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..tools.types import RiskLevel, ToolCapability
from . import allowlist, command_paths, rules
from .messages import (
    NO_PERMISSION_RULE_MSG,
    PLAN_EXEC_MSG,
    PLAN_WRITE_MSG,
)
from .paths import write_target_display
from .shell import is_readonly_command
from .types import ApprovalDecision, ApprovalRequest, DenyReason, PermissionMode, PermissionOutcome

if TYPE_CHECKING:
    from ..session import WorkspaceAccess
    from ..tools.base import Tool

_CRITICAL_RISK_MSG = "该操作被标记为 CRITICAL 风险，每次都需要单独确认。"


class PermissionManager:
    """按模式与规则表判定一次工具调用该放行、拒绝还是等人审批。"""

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace).resolve()

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

        # ---- 1. Blocked：永远 DENY，连 Bypass 也不能绕过 -----------------
        if tool.capability is ToolCapability.EXEC:
            blocked_reason = rules.blocked_command(str(args.get("command") or ""))
            if blocked_reason:
                return PermissionOutcome.deny(DenyReason.POLICY_BLOCKED, blocked_reason)

        # ---- 2. Restricted / 路径沙箱：DENY，不出审批 UI，同样不受 Bypass 影响
        if tool.capability is ToolCapability.EXEC:
            outside = command_paths.outside_workspace_reason(
                self.workspace, str(args.get("command") or ""), self._exec_cwd(workspace_access)
            )
            if outside:
                return PermissionOutcome.deny(DenyReason.POLICY_BLOCKED, outside)

        restricted_reason = self._restricted_reason(tool, args, workspace_access)
        if restricted_reason:
            return PermissionOutcome.deny(DenyReason.RESTRICTED, restricted_reason)

        if tool.capability is ToolCapability.READ:
            forced = self._forced_confirmation_reason(tool, args, workspace_access)
            if forced:
                return self._need_approval(
                    tool, args, force=True, reason=forced, workspace_access=workspace_access
                )
            return PermissionOutcome.allow()

        if mode is PermissionMode.PLAN:
            if tool.capability is ToolCapability.WRITE:
                return PermissionOutcome.deny(DenyReason.MODE_BLOCKED, PLAN_WRITE_MSG)
            if tool.capability is ToolCapability.EXEC:
                command = str(args.get("command") or "")
                if not is_readonly_command(command):
                    return PermissionOutcome.deny(DenyReason.MODE_BLOCKED, PLAN_EXEC_MSG)
                forced = command_paths.forced_exec_reason(
                    self.workspace, command, self._exec_cwd(workspace_access)
                )
                if forced:
                    return self._need_approval(
                        tool, args, force=True, reason=forced, workspace_access=workspace_access
                    )
                return PermissionOutcome.allow()

        # ---- 3. Sensitive / CRITICAL：强制确认（Bypass / 白名单也不能跳过）
        forced_reason = self._forced_confirmation_reason(tool, args, workspace_access)
        if forced_reason:
            return self._need_approval(
                tool, args, force=True, reason=forced_reason, workspace_access=workspace_access
            )

        # ---- 4. Normal：Bypass / AcceptEdits / 白名单 / 普通审批 -------------
        if mode is PermissionMode.BYPASS:
            return PermissionOutcome.allow()

        if mode is PermissionMode.ACCEPT_EDITS and tool.capability is ToolCapability.WRITE:
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
        """落实审批结果。``ALLOW_ALWAYS`` 写入白名单；``force`` 时只允许这一次。"""
        if decision is ApprovalDecision.ALLOW_ALWAYS and not force:
            allowlist.remember(self.workspace, tool, args, always_allowed)
        return decision is not ApprovalDecision.DENY

    def _exec_cwd(self, workspace_access: WorkspaceAccess | None) -> Path:
        if workspace_access is not None and workspace_access.bash_cwd is not None:
            return workspace_access.bash_cwd
        return self.workspace

    def _restricted_reason(
        self, tool: Tool, args: dict[str, Any], workspace_access: WorkspaceAccess | None = None
    ) -> str | None:
        """强硬限制：命中则直接 DENY，不进审批面板。"""
        if tool.capability is ToolCapability.EXEC:
            command = str(args.get("command") or "")
            return rules.restricted_command(command) or command_paths.restricted_write_reason(
                self.workspace, command, self._exec_cwd(workspace_access)
            )
        if tool.capability is ToolCapability.WRITE:
            target = write_target_display(self.workspace, args)
            if target is not None:
                return rules.restricted_path(target)
        return None

    def _forced_confirmation_reason(
        self, tool: Tool, args: dict[str, Any], workspace_access: WorkspaceAccess | None = None
    ) -> str | None:
        """敏感 / CRITICAL：必须逐次确认，不能被始终允许或 Bypass 跳过。"""
        if tool.risk is RiskLevel.CRITICAL:
            return _CRITICAL_RISK_MSG
        if tool.capability is ToolCapability.EXEC:
            command = str(args.get("command") or "")
            return rules.sensitive_command(command) or command_paths.forced_exec_reason(
                self.workspace, command, self._exec_cwd(workspace_access)
            )
        if tool.capability is ToolCapability.WRITE:
            target = write_target_display(self.workspace, args)
            if target is not None:
                return rules.sensitive_path(target)
        if tool.capability is ToolCapability.READ:
            target = write_target_display(self.workspace, args)
            if target is not None and rules.sensitive_path(target):
                return f"{target} 可能包含密钥 / 凭据，读取也需要确认。"
        return None

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
            risk=tool.risk.value,
            summary=summary,
            detail=detail,
            detail_format=detail_format,
            force=force,
            reason=reason,
            always_label=None if force else allowlist.always_label(self.workspace, tool, args),
        )
        return PermissionOutcome.need_approval(request)
