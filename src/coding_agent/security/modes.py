"""Permission 模式。

Layer 2：在工具 capability 与安全规则之上，决定默认放行策略。
"""

from __future__ import annotations

from enum import Enum


class PermissionMode(Enum):
    PLAN = "plan"
    DEFAULT = "default"
    ACCEPT_EDITS = "accept_edits"
    BYPASS = "bypass"

    @classmethod
    def parse(cls, raw: str) -> PermissionMode:
        normalized = str(raw).strip().lower().replace("-", "_")
        try:
            return cls(normalized)
        except ValueError as exc:
            raise ValueError(
                f"未知权限模式 {raw!r}, 可选: plan / default / accept_edits / bypass"
            ) from exc
