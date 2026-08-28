"""Session 权限数据，不负责判断逻辑。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SessionPermissions:
    always_allowed: set[str] = field(default_factory=set)
    # TODO: denied / rules 接入 PermissionManager 持久化拒绝与用户自定义规则
    denied: set[str] = field(default_factory=set)
    rules: dict[str, Any] = field(default_factory=dict)
