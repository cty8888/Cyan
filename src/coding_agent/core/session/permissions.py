"""Session 权限数据，不负责判断逻辑。"""

from __future__ import annotations

from dataclasses import dataclass, field

from ...security.modes import PermissionMode


@dataclass
class SessionPermissions:
    permission_mode: PermissionMode = PermissionMode.DEFAULT
    always_allowed: set[str] = field(default_factory=set)
