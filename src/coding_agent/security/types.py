"""安全层的数据契约：权限模式、审批协议与判定结果。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal


class PermissionMode(Enum):
    PLAN = "plan"
    DEFAULT = "default"
    ACCEPT_EDITS = "accept_edits"
    BYPASS = "bypass"


class ApprovalDecision(Enum):
    ALLOW_ONCE = "once"
    ALLOW_ALWAYS = "always"
    DENY = "deny"


class DenyReason(Enum):
    MODE_BLOCKED = "mode"
    POLICY_BLOCKED = "policy"
    RESTRICTED = "restricted"
    UNKNOWN = "unknown"
    USER_DENIED = "user"


@dataclass
class ApprovalRequest:
    """一次待确认的高风险操作。"""

    tool_name: str
    capability: str
    risk: str
    summary: str
    detail: str | None = None
    detail_format: str = "text"
    force: bool = False
    reason: str | None = None
    always_label: str | None = None


@dataclass
class PermissionOutcome:
    """单次工具调用的权限判定结果。"""

    kind: Literal["allow", "deny", "need_approval"]
    deny_reason: DenyReason | None = None
    deny_message: str | None = None
    request: ApprovalRequest | None = None

    @classmethod
    def allow(cls) -> PermissionOutcome:
        return cls(kind="allow")

    @classmethod
    def deny(cls, reason: DenyReason, message: str) -> PermissionOutcome:
        return cls(kind="deny", deny_reason=reason, deny_message=message)

    @classmethod
    def need_approval(cls, request: ApprovalRequest) -> PermissionOutcome:
        return cls(kind="need_approval", request=request)
