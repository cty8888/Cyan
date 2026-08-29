"""权限管理：结合 Permission 模式判定 ALLOW / DENY / NEED_APPROVAL。

四级判定链（对应 docs/architecture.md「安全模型」一节）：

1. **Blocked（黑名单）**：永远 DENY，任何模式（包括 Bypass）都不能绕过。
2. **Restricted（强硬限制）**：DENY，不出审批 UI；同样不受 Bypass / 「始终允许」影响。
3. **Sensitive（敏感）**：NEED_APPROVAL 且 force=True，不受「始终允许」或
   AcceptEdits / Bypass 模式影响。``RiskLevel.CRITICAL`` 同等处理。
4. **Normal（普通）**：由 PermissionMode 现有逻辑处理。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..tools.types import RiskLevel, ToolCapability
from . import rules
from .messages import (
    NO_PERMISSION_RULE_MSG,
    PLAN_EXEC_MSG,
    PLAN_WRITE_MSG,
)
from .paths import display, resolve_path
from .readonly import command_head, is_readonly_command
from .types import ApprovalDecision, ApprovalRequest, DenyReason, PermissionMode, PermissionOutcome

if TYPE_CHECKING:
    from ..session import WorkspaceAccess
    from ..tools.base import Tool

_CRITICAL_RISK_MSG = "该操作被标记为 CRITICAL 风险，每次都需要单独确认。"
_WRITE_SCOPE_PREFIX = "write:"
_EXEC_SCOPE_PREFIX = "exec:"


class PermissionManager:
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

        # ---- 2. Restricted：DENY，不出审批 UI，同样不受 Bypass 影响 ------
        restricted_reason = self._restricted_reason(tool, args)
        if restricted_reason:
            return PermissionOutcome.deny(DenyReason.RESTRICTED, restricted_reason)

        if mode is PermissionMode.BYPASS:
            # Bypass 只跳过「普通」级别的审批；Sensitive/CRITICAL 仍然逐次确认，
            # 因为它们代表的是"这类操作本身值得再看一眼"，跟执行模式无关。
            forced_reason = self._forced_confirmation_reason(tool, args)
            if forced_reason:
                return self._need_approval(
                    tool, args, force=True, reason=forced_reason, workspace_access=workspace_access
                )
            return PermissionOutcome.allow()

        if tool.capability is ToolCapability.READ:
            return PermissionOutcome.allow()

        if mode is PermissionMode.PLAN:
            if tool.capability is ToolCapability.WRITE:
                return PermissionOutcome.deny(DenyReason.MODE_BLOCKED, PLAN_WRITE_MSG)
            if tool.capability is ToolCapability.EXEC:
                command = str(args.get("command") or "")
                if is_readonly_command(command):
                    return PermissionOutcome.allow()
                return PermissionOutcome.deny(DenyReason.MODE_BLOCKED, PLAN_EXEC_MSG)

        # ---- 3. Sensitive / CRITICAL：强制确认，不受始终允许或模式影响 ----
        forced_reason = self._forced_confirmation_reason(tool, args)
        if forced_reason:
            return self._need_approval(
                tool, args, force=True, reason=forced_reason, workspace_access=workspace_access
            )

        # ---- 4. Normal：维持现有按模式 / 白名单判定的逻辑 -----------------
        if mode is PermissionMode.ACCEPT_EDITS and tool.capability is ToolCapability.WRITE:
            return PermissionOutcome.allow()

        if self.is_always_allowed(tool, args, always_allowed):
            return PermissionOutcome.allow()

        if tool.capability in {
            ToolCapability.WRITE,
            ToolCapability.EXEC,
        }:
            return self._need_approval(
                tool, args, force=False, reason=None, workspace_access=workspace_access
            )

        return PermissionOutcome.deny(DenyReason.UNKNOWN, NO_PERMISSION_RULE_MSG)

    def is_always_allowed(self, tool: Tool, args: dict[str, Any], always_allowed: set[str]) -> bool:
        """白名单按「同类操作」匹配，而不是整个工具名。

        - 写入：同一目录及其子目录（``write:pkg`` 能放行 ``pkg/a.py`` 和 ``pkg/sub/b.py``，
          不能放行仓库根下的 ``ok.py``）。
        - 执行：同一命令名（``exec:pytest`` 能放行 ``pytest -q``，不能放行 ``touch``）。
        """
        if tool.capability is ToolCapability.WRITE:
            current = self._write_dir_scope(args)
            if current is None:
                return False
            for key in always_allowed:
                if key.startswith(_WRITE_SCOPE_PREFIX) and _write_dir_matches(
                    key[len(_WRITE_SCOPE_PREFIX) :], current
                ):
                    return True
            return False
        if tool.capability is ToolCapability.EXEC:
            head = command_head(str(args.get("command") or ""))
            return bool(head) and f"{_EXEC_SCOPE_PREFIX}{head}" in always_allowed
        return tool.name in always_allowed

    def remember_always_allowed(self, tool: Tool, args: dict[str, Any], always_allowed: set[str]) -> None:
        key = self._always_key(tool, args)
        if key:
            always_allowed.add(key)

    def _always_key(self, tool: Tool, args: dict[str, Any]) -> str | None:
        if tool.capability is ToolCapability.WRITE:
            scope = self._write_dir_scope(args)
            return f"{_WRITE_SCOPE_PREFIX}{scope}" if scope is not None else None
        if tool.capability is ToolCapability.EXEC:
            head = command_head(str(args.get("command") or ""))
            return f"{_EXEC_SCOPE_PREFIX}{head}" if head else None
        return tool.name

    def always_label(self, tool: Tool, args: dict[str, Any]) -> str | None:
        """审批面板上「始终允许」对应的范围说明。"""
        if tool.capability is ToolCapability.WRITE:
            scope = self._write_dir_scope(args)
            if scope is None:
                return None
            if scope == ".":
                return "工作目录根下的写入"
            return f"{scope}/ 下的写入"
        if tool.capability is ToolCapability.EXEC:
            head = command_head(str(args.get("command") or ""))
            return f"{head} 命令" if head else None
        return None

    def _write_dir_scope(self, args: dict[str, Any]) -> str | None:
        """写入目标所在目录（相对工作区）。根目录文件记为 ``.``。"""
        target = self._write_target_display(args)
        if target is None:
            return None
        text = target.replace("\\", "/")
        while text.startswith("./"):
            text = text[2:]
        if not text or text == ".":
            return "."
        if "/" not in text:
            return "."
        return text.rsplit("/", 1)[0]

    def _restricted_reason(self, tool: Tool, args: dict[str, Any]) -> str | None:
        if tool.capability is ToolCapability.EXEC:
            return rules.restricted_command(str(args.get("command") or ""))
        if tool.capability is ToolCapability.WRITE:
            target = self._write_target_display(args)
            if target is not None:
                return rules.restricted_path(target)
        return None

    def _forced_confirmation_reason(self, tool: Tool, args: dict[str, Any]) -> str | None:
        if tool.risk is RiskLevel.CRITICAL:
            return _CRITICAL_RISK_MSG
        if tool.capability is ToolCapability.EXEC:
            return rules.sensitive_command(str(args.get("command") or ""))
        if tool.capability is ToolCapability.WRITE:
            target = self._write_target_display(args)
            if target is not None:
                return rules.sensitive_path(target)
        return None

    def _write_target_display(self, args: dict[str, Any]) -> str | None:
        """把 write 类工具的 path 参数解析成相对工作目录的展示路径，供规则匹配。

        解析失败（比如路径本身就非法）时退回原始字符串，只影响规则匹配的准确性，
        不在这里抛异常——真正的路径合法性校验交给工具自己的 resolve_path。
        """
        raw = args.get("path")
        if raw is None:
            return None
        raw = str(raw)
        try:
            resolved = resolve_path(self.workspace, raw)
        except Exception:
            return raw
        return display(self.workspace, resolved)

    def _need_approval(
        self,
        tool: Tool,
        args: dict[str, Any],
        *,
        force: bool,
        reason: str | None,
        workspace_access: WorkspaceAccess | None = None,
    ) -> PermissionOutcome:
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
            always_label=None if force else self.always_label(tool, args),
        )
        return PermissionOutcome.need_approval(request)

    def apply_decision(
        self,
        decision: ApprovalDecision,
        tool: Tool,
        args: dict[str, Any],
        always_allowed: set[str],
    ) -> bool:
        if decision is ApprovalDecision.ALLOW_ALWAYS:
            self.remember_always_allowed(tool, args, always_allowed)
        return decision is not ApprovalDecision.DENY


def _write_dir_matches(allowed_dir: str, current_dir: str) -> bool:
    if allowed_dir == ".":
        return current_dir == "."
    return current_dir == allowed_dir or current_dir.startswith(allowed_dir + "/")
