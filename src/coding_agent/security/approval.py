"""审批协议。

core 层只产出 ``ApprovalRequest`` 并等待一个 ``ApprovalDecision``，
具体怎么问用户由 CLI 层决定，因此内核不依赖任何输入输出设施。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ApprovalDecision(Enum):
    ALLOW_ONCE = "once"
    ALLOW_ALWAYS = "always"
    DENY = "deny"


@dataclass
class ApprovalRequest:
    """一次待确认的高风险操作。"""

    tool_name: str
    risk: str
    summary: str
    # 供人阅读的细节：命令全文、文件 diff 等
    detail: str | None = None
    detail_format: str = "text"
    # 强制确认的操作（敏感文件、危险目录）不受 --yolo 和「本会话始终允许」影响
    force: bool = False
    reason: str | None = None
