"""权限管理：结合 Permission 模式判定 ALLOW / DENY / NEED_APPROVAL。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..constants.security.messages import (
    NO_PERMISSION_RULE_MSG,
    PLAN_EXEC_MSG,
    PLAN_WRITE_MSG,
    USER_DENIED_MSG,
)
from ..tools.base import ToolCapability
from .approval import ApprovalRequest, DenyReason, PermissionOutcome
from .utils import is_readonly_command
from .modes import PermissionMode

if TYPE_CHECKING:
    from ..tools.base import Tool


class PermissionManager:
    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()

    def evaluate(
        self,
        tool: Tool,
        args: dict[str, Any],
        *,
        mode: PermissionMode,
        always_allowed: set[str],
    ) -> PermissionOutcome:
        """判定本次工具调用的权限结果。"""

        if mode is PermissionMode.BYPASS:
            return PermissionOutcome.allow()

        if tool.capability is ToolCapability.READ:
            return PermissionOutcome.allow()

        if mode is PermissionMode.PLAN:
            if tool.capability is ToolCapability.WRITE:
                return PermissionOutcome.deny(DenyReason.MODE_BLOCKED,PLAN_WRITE_MSG,)
            if tool.capability is ToolCapability.EXEC:
                command = str(args.get("command") or "")
                if is_readonly_command(command):
                    return PermissionOutcome.allow()
                return PermissionOutcome.deny(DenyReason.MODE_BLOCKED,PLAN_EXEC_MSG,)

        if mode is PermissionMode.ACCEPT_EDITS and tool.capability is ToolCapability.WRITE:
            return PermissionOutcome.allow()

        if tool.name in always_allowed:
            return PermissionOutcome.allow()

        if tool.capability in {
            ToolCapability.WRITE,
            ToolCapability.EXEC,
        }:
            return self._need_approval(tool,args,force=False,reason=None,)

        return PermissionOutcome.deny(DenyReason.UNKNOWN, NO_PERMISSION_RULE_MSG)

    def _need_approval(
        self,
        tool: Tool,
        args: dict[str, Any],
        *,
        force: bool,
        reason: str | None,
    ) -> PermissionOutcome:
        summary, detail, detail_format = tool.describe(args, self.workspace)
        request = ApprovalRequest(
            tool_name=tool.name,
            capability=tool.capability.value,
            risk=tool.risk.value,
            summary=summary,
            detail=detail,
            detail_format=detail_format,
            force=force,
            reason=reason,
        )
        return PermissionOutcome.need_approval(request)

    @staticmethod
    def apply_decision(
        decision,
        tool_name: str,
        always_allowed: set[str],
    ) -> bool:
        from .approval import ApprovalDecision

        if decision is ApprovalDecision.ALLOW_ALWAYS:
            always_allowed.add(tool_name)
        return decision is not ApprovalDecision.DENY

    @staticmethod
    def user_denied_message() -> str:
        return USER_DENIED_MSG
