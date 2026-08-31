"""安全层的数据契约：权限模式、审批协议与判定结果。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal


class PermissionMode(Enum):
    """会话级权限模式，决定写文件 / 执行命令默认是放行、拒绝还是要审批。"""

    PLAN = "plan"  # 只读规划：禁止写文件，bash 仅放行只读命令
    DEFAULT = "default"  # 写/执行需逐次确认
    ACCEPT_EDITS = "accept_edits"  # 自动批准普通写入，执行仍要确认


class ApprovalDecision(Enum):
    """用户在审批面板上的选择。"""

    ALLOW_ONCE = "once"
    ALLOW_ALWAYS = "always"  # 本会话记住同类操作（同目录写入 / 同命令）
    DENY = "deny"


class DenyReason(Enum):
    """硬拒绝的分类，供日志与后续扩展；目前主要 internally 区分来源。"""

    MODE_BLOCKED = "mode"  # 当前 PermissionMode 不允许
    POLICY_BLOCKED = "policy"  # 命中黑名单
    RESTRICTED = "restricted"  # 强硬限制，不出审批 UI
    UNKNOWN = "unknown"
    USER_DENIED = "user"


@dataclass
class ApprovalRequest:
    """一次待确认的高风险操作。"""

    tool_name: str
    capability: str
    summary: str
    detail: str | None = None  # diff / 命令原文等，按 detail_format 渲染
    detail_format: str = "text"
    force: bool = False  # True 时不能选「始终允许」
    reason: str | None = None  # 强制确认的额外说明（敏感路径、不透明命令）
    always_label: str | None = None  # 「a=始终允许」的范围文案


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
