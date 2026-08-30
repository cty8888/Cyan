"""对话压缩策略的启动默认值。

优先级与其它域相同：命令行 / 环境变量 > 本文件默认值。
Runtime 持有一份副本，会话中途改 ``runtime.compact_policy`` 不影响 ``AgentSettings``。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CompactPolicy:
    """何时压、留几轮。不存放会话内容。"""

    max_context_tokens: int = 256_000
    reserve_tokens: int = 3_000  # 给总结那次 chat 留口，不存放内容
    trigger_ratio: float = 0.9  # 阈值 = (max - reserve) * ratio
    keep_recent_turns: int = 2  # 保留段：最近几轮 Assistant（同一任务内较早的工具轮可压）
