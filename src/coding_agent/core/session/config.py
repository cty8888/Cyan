"""Session 级配置快照。

只保留与会话本身相关的配置；模型参数（model / temperature）留在应用级 ``Config``，
上下文参数放在 ``ContextConfig``，系统提示词作为 ``messages`` 第一条 ``SystemMessage`` 保存。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SessionConfig:
    """占位，预留给未来会话级配置项。"""

    pass
