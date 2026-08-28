"""权限管理：结合执行模式与安全规则，判定 ALLOW / DENY / NEED_APPROVAL。

``SecurityPolicy`` 负责「触碰哪条安全边界」；
``PermissionManager`` 负责「最终要不要问用户」。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..tools.base import RiskLevel
from .approval import ApprovalRequest, DenyReason, PermissionOutcome
from .modes import ExecutionMode
from .policy import SecurityPolicy

if TYPE_CHECKING:
    from ..tools.base import Tool

_MODE_BLOCKED_MSG = (
    "当前处于 Ask 模式，不允许修改文件或执行命令。"
    "请向用户说明所需操作，或建议切换到 Agent 模式。"
)
_USER_DENIED_MSG = "用户拒绝了此操作。请不要重试，改用其他方案或询问用户的意见。"


class PermissionManager:
    def __init__(self, policy: SecurityPolicy):
        self.policy = policy

    def evaluate(
        self,
        tool: Tool,
        args: dict[str, Any],
        *,
        mode: ExecutionMode,
        always_allowed: set[str],
    ) -> PermissionOutcome:
        """判定本次工具调用的处置结果。"""
        blocked = self.policy.blocked_concern(tool, args)
        if blocked:
            detail = args.get("command") or args.get("path") or ""
            message = (
                f"操作被安全策略拦截（{blocked}）"
                f"{('：' + str(detail)) if detail else ''}。"
                "该限制无法通过授权绕过，请改用更安全的做法。"
            )
            return PermissionOutcome.deny(DenyReason.POLICY_BLOCKED, message)

        if mode is ExecutionMode.ASK and tool.risk is not RiskLevel.READ:
            return PermissionOutcome.deny(DenyReason.MODE_BLOCKED, _MODE_BLOCKED_MSG)

        restricted = self.policy.restricted_concern(tool, args)
        if restricted:
            detail = args.get("command") or args.get("path") or ""
            message = (
                f"操作被强硬限制策略拦截（{restricted}）"
                f"{('：' + str(detail)) if detail else ''}。"
                "该操作不允许执行，即使用户授权也无法绕过。请改用更安全的替代方案。"
            )
            return PermissionOutcome.deny(DenyReason.RESTRICTED, message)

        sensitive = self.policy.sensitive_concern(tool, args)
        if sensitive:
            return self._need_approval(tool, args, force=True, reason=sensitive)

        if tool.risk is RiskLevel.READ:
            return PermissionOutcome.allow()

        if tool.risk is RiskLevel.WRITE:
            return PermissionOutcome.allow()

        # EXEC normal
        if mode is ExecutionMode.YOLO:
            return PermissionOutcome.allow()
        if tool.name in always_allowed:
            return PermissionOutcome.allow()
        return self._need_approval(tool, args, force=False, reason=None)

    def _need_approval(
        self,
        tool: Tool,
        args: dict[str, Any],
        *,
        force: bool,
        reason: str | None,
    ) -> PermissionOutcome:
        summary, detail, detail_format = tool.describe(args, self.policy)
        request = ApprovalRequest(
            tool_name=tool.name,
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
        return _USER_DENIED_MSG
